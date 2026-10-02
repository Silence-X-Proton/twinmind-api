#!/usr/bin/env python3
"""
TwinMind -> OpenAI-compatible API server (premium build).

Endpoints:
  GET  /v1/models
  GET  /v1/models/{id}
  POST /v1/chat/completions        (stream + non-stream)
  POST /v1/completions             (legacy)
  GET  /health
  GET  /admin/stats
  POST /admin/accounts/add
  POST /admin/accounts/rotate      (burn oldest + replace)
  POST /admin/accounts/burn-all    (burn every account + refill)

Reliability / "never hit a limit":
  * Every request is served by an account from a rotating AccountPool.
  * Any 401/403/429/5xx/timeout -> automatic retry on the NEXT account.
  * Accounts are circuit-broken and auto-replaced on repeated failure.
  * BURN-AFTER-USE: after an account has served TWINMIND_REQ_PER_ACCOUNT
    requests it is deleted upstream (user + Firebase) and replaced with a
    fresh one -> no trace left behind.
  * Optional scheduled rotation burns idle accounts on an interval.

Premium streaming:
  * Long-lived SSE with heartbeat comments so intermediaries never drop an
    idle-but-alive stream (no mid-stream break on heavy/long answers).
  * Thinking tokens surfaced as reasoning_content; large outputs supported.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Iterable, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from accounts import AccountPool

# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
BACKEND = os.environ.get("TWINMIND_BACKEND", "https://api2.twinmind.com")
CHAT_PATH = "/api/v3/chat"
MODELS_PATH = "/api/v3/chat/models"

POOL_SIZE = int(os.environ.get("TWINMIND_POOL_SIZE", "15"))
STATE_FILE = os.environ.get(
    "TWINMIND_STATE_FILE", os.path.join(os.path.dirname(__file__), "pool_state.json")
)
MAX_RETRIES = int(os.environ.get("TWINMIND_MAX_RETRIES", "8"))
REQ_TIMEOUT = float(os.environ.get("TWINMIND_TIMEOUT", "900"))
SERVE_HOST = os.environ.get("TWINMIND_HOST", "0.0.0.0")
SERVE_PORT = int(os.environ.get("TWINMIND_PORT", "8080"))
API_KEYS = {k for k in os.environ.get("TWINMIND_API_KEYS", "").split(",") if k}
DEFAULT_MODEL = os.environ.get("TWINMIND_DEFAULT_MODEL", "auto")

# Burn-after-use: after this many served requests an account is deleted and
# replaced. 0 disables (account lives for the whole server lifetime).
REQ_PER_ACCOUNT = int(os.environ.get("TWINMIND_REQ_PER_ACCOUNT", "300"))
# Optional background hygiene: burn the least-recently-used account and replace
# it every N seconds. 0 disables.
ROTATE_INTERVAL = int(os.environ.get("TWINMIND_ROTATE_INTERVAL", "0"))
# Heartbeat cadence (seconds) for streaming responses.
HEARTBEAT = float(os.environ.get("TWINMIND_HEARTBEAT", "15"))

# Baked-in catalog (fallback when upstream models endpoint is unreachable).
STATIC_CATALOG = [
    ("google", "Google", [
        ("gemini-3.1-pro-thinking", "Gemini 3.1 Pro Thinking", "max"),
        ("gemini-3.8-flash-thinking", "Gemini 3.8 Flash Thinking", "max"),
        ("gemini-3.7-flash", "Gemini 3.7 Flash", "pro"),
        ("gemini-3.6-flash", "Gemini 3.6 Flash", "pro"),
    ]),
    ("openai", "OpenAI", [
        ("gpt-6-astra-thinking", "GPT-6 Astra Thinking", "max"),
        ("gpt-6.1-sol-thinking", "GPT-6.1 Sol Thinking", "max"),
        ("gpt-6-luna", "GPT-6 Luna", "pro"),
        ("gpt-5.6-terra", "GPT-5.6 Terra", "pro"),
    ]),
    ("anthropic", "Anthropic", [
        ("claude-opus-5-5-thinking", "Claude Opus 5.5 Thinking", "max"),
        ("claude-opus-5-thinking", "Claude Opus 5 Thinking", "max"),
        ("claude-sonnet-5-5", "Claude Sonnet 5.5", "pro"),
        ("claude-sonnet-5", "Claude Sonnet 5", "pro"),
    ]),
]

pool = AccountPool(size=POOL_SIZE, state_file=STATE_FILE)


# --------------------------------------------------------------------------- #
# Background hygiene                                                           #
# --------------------------------------------------------------------------- #
async def _rotation_loop() -> None:
    while True:
        await asyncio.sleep(max(30, ROTATE_INTERVAL))
        try:
            await asyncio.to_thread(pool.rotate)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if ROTATE_INTERVAL > 0:
        task = asyncio.create_task(_rotation_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="TwinMind OpenAI-Compatible API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
_TRANSIENT_STATUS = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


def _norm_model(model: str | None) -> str:
    if not model:
        return DEFAULT_MODEL
    m = model.strip()
    aliases = {
        "gpt-4": "gpt-6-luna",
        "gpt-4o": "gpt-6-luna",
        "gpt-4o-mini": "gpt-6-luna",
        "gpt-3.5-turbo": "gemini-3.6-flash",
        "gpt-5": "gpt-6-luna",
        "o1": "gpt-6-astra-thinking",
        "claude-3": "claude-sonnet-5",
        "claude-3-opus": "claude-opus-5-thinking",
        "claude-3-sonnet": "claude-sonnet-5",
        "claude-3-haiku": "claude-sonnet-5",
        "gemini": "gemini-3.6-flash",
    }
    return aliases.get(m, m)


def _check_auth(authorization: Optional[str]) -> None:
    if not API_KEYS:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing API key")
    token = authorization.removeprefix("Bearer ").strip()
    if token not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _flatten_messages(messages: Iterable[dict]) -> str:
    parts: list[str] = []
    for m in messages:
        role = (m.get("role") or "user").lower()
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )
        if not content:
            continue
        if role == "system":
            parts.append(f"[System] {content}")
        elif role == "assistant":
            parts.append(f"[Assistant] {content}")
        else:
            parts.append(f"[User] {content}")
    return "\n".join(parts).strip() or "Hello"


def _build_payload(query: str, model: str, session_id: Optional[str], extra: dict | None = None) -> dict:
    body: dict[str, Any] = {
        "type": "app", "version": 1, "response_version": 1,
        "query": query,
        "model": {"model_name": model} if model and model != "auto" else "auto",
        "context": None,
        "client": {
            "platform": "web",
            "timezone": os.environ.get("TWINMIND_TZ", "Asia/Kolkata"),
            "client_time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "locale": "en-US",
        },
        "mode": "default",
    }
    if session_id:
        body["session_id"] = session_id
    if extra:
        body.update(extra)
    return body


def _parse_sse_block(block: str) -> Optional[dict]:
    block = block.strip()
    if not block:
        return None
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None
    return None


async def _open_stream(client: httpx.AsyncClient, token: str, payload: dict) -> httpx.Response:
    req = client.build_request(
        "POST", BACKEND + CHAT_PATH, json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        timeout=REQ_TIMEOUT,
    )
    return await client.send(req, stream=True)


async def _sleep(attempt: int) -> None:
    await asyncio.sleep(min(0.25 * (2 ** attempt), 3.0))


def _account_done(acc) -> None:
    """Burn-after-use: retire an account that has served its quota."""
    if REQ_PER_ACCOUNT and acc.total_requests >= REQ_PER_ACCOUNT:
        asyncio.get_event_loop().run_in_executor(None, pool.burn_and_replace, acc)


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health():
    s = pool.stats()
    return {"status": "ok", "pool": {"size": s["size"], "available": s["available"]}}


@app.get("/admin/stats")
async def admin_stats(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return pool.stats()


@app.post("/admin/accounts/add")
async def admin_add_account(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return pool.add_account()


@app.post("/admin/accounts/rotate")
async def admin_rotate(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    nxt = await asyncio.to_thread(pool.rotate)
    return {"rotated": bool(nxt), "account": nxt.snapshot() if nxt else None}


@app.post("/admin/accounts/burn-all")
async def admin_burn_all(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    burned = 0
    for a in list(pool._accounts):
        try:
            await asyncio.to_thread(pool.burn_and_replace, a)
            burned += 1
        except Exception:
            pass
    return {"burned": burned, "pool_size": pool.stats()["size"]}


@app.get("/v1/models")
async def list_models(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    created = int(time.time())
    last_err = "unknown"
    async with httpx.AsyncClient(timeout=REQ_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES):
            acc = pool.acquire()
            try:
                token = acc.ensure_token()
            except Exception as e:
                acc.mark_fail(hard=True, cooldown=30)
                last_err = f"auth: {e}"
                continue
            try:
                resp = await client.get(BACKEND + MODELS_PATH, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                acc.mark_fail(cooldown=5)
                last_err = f"transport: {e}"
                await _sleep(attempt)
                continue
            if resp.status_code >= 400:
                acc.mark_fail(hard=resp.status_code in (401, 403), cooldown=5)
                last_err = f"status {resp.status_code}"
                await _sleep(attempt)
                continue
            acc.mark_ok()
            try:
                upstream = json.loads(resp.text)
            except Exception:
                upstream = {}
            data = []
            for prov in upstream.get("providers", []):
                for m in prov.get("models", []):
                    data.append({
                        "id": m["name"], "object": "model", "created": created,
                        "owned_by": prov.get("id", "twinmind"), "permission": [],
                        "root": m["name"], "parent": None,
                        "display_name": m.get("display_name"),
                        "minimum_tier": m.get("minimum_tier"),
                    })
            data.append({"id": "auto", "object": "model", "created": created,
                         "owned_by": "twinmind", "permission": []})
            return {"object": "list", "data": data}
    # fallback catalog
    data = []
    for prov_id, _name, mods in STATIC_CATALOG:
        for name, disp, tier in mods:
            data.append({
                "id": name, "object": "model", "created": created,
                "owned_by": prov_id, "permission": [], "root": name,
                "parent": None, "display_name": disp, "minimum_tier": tier,
            })
    data.append({"id": "auto", "object": "model", "created": created,
                 "owned_by": "twinmind", "permission": []})
    return {"object": "list", "data": data}


@app.get("/v1/models/{model_id}")
async def get_model(model_id: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"id": model_id, "object": "model", "created": int(time.time()), "owned_by": "twinmind"}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_twinmind_session: Optional[str] = Header(None),
):
    _check_auth(authorization)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="'messages' must be a non-empty list")

    model = _norm_model(body.get("model"))
    stream = bool(body.get("stream", False))
    query = _flatten_messages(messages)
    session_id = x_twinmind_session or body.get("session_id")
    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if not stream:
        async def collect(resp: httpx.Response):
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            sid = session_id
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                blocks = buf.split("\n\n")
                buf = blocks.pop()
                for b in blocks:
                    ev = _parse_sse_block(b)
                    if not ev:
                        continue
                    t = ev.get("type")
                    if t == "run_start":
                        sid = ev.get("session_id", sid)
                    elif t == "text_delta":
                        text_parts.append(ev.get("content", ""))
                    elif t == "thinking_delta":
                        thinking_parts.append(ev.get("content", ""))
            await resp.aclose()
            text = "".join(text_parts)
            msg = {"role": "assistant", "content": text}
            if thinking_parts:
                msg["reasoning_content"] = "".join(thinking_parts)
            if sid:
                msg["twinmind_session"] = sid
            return {
                "id": response_id, "object": "chat.completion", "created": created,
                "model": model,
                "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": max(1, len(query) // 4),
                    "completion_tokens": max(1, len(text) // 4),
                    "total_tokens": max(1, len(query) // 4) + max(1, len(text) // 4),
                },
            }

        last_err = "unknown"
        async with httpx.AsyncClient(timeout=REQ_TIMEOUT) as client:
            for attempt in range(MAX_RETRIES):
                acc = pool.acquire()
                try:
                    token = acc.ensure_token()
                except Exception as e:
                    acc.mark_fail(hard=True, cooldown=30)
                    last_err = f"auth: {e}"
                    continue
                try:
                    resp = await _open_stream(client, token, _build_payload(query, model, session_id))
                except httpx.HTTPError as e:
                    acc.mark_fail(cooldown=5)
                    last_err = f"transport: {e}"
                    await _sleep(attempt)
                    continue
                if resp.status_code >= 400:
                    body_txt = (await resp.aread()).decode("utf-8", "replace")[:300]
                    await resp.aclose()
                    hard = resp.status_code in (401, 403)
                    acc.mark_fail(hard=hard, cooldown=10 if hard else 3)
                    last_err = f"status {resp.status_code}: {body_txt}"
                    if hard and acc.failures >= 2:
                        pool.recycle(acc, burn=True)
                    await _sleep(attempt)
                    continue
                acc.mark_ok()
                result = await collect(resp)
                _account_done(acc)
                return result
        raise HTTPException(status_code=502, detail=f"All accounts failed. Last: {last_err}")

    # ---------------- streaming ----------------
    def frame(delta: dict, finish=None) -> str:
        obj = {
            "id": response_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(obj)}\n\n"

    async def stream_gen() -> AsyncGenerator[str, None]:
        last_err = "unknown"
        for attempt in range(MAX_RETRIES):
            acc = pool.acquire()
            try:
                token = acc.ensure_token()
            except Exception as e:
                acc.mark_fail(hard=True, cooldown=30)
                last_err = f"auth: {e}"
                continue
            client = httpx.AsyncClient(timeout=REQ_TIMEOUT)
            try:
                resp = await _open_stream(client, token, _build_payload(query, model, session_id))
            except httpx.HTTPError as e:
                await client.aclose()
                acc.mark_fail(cooldown=5)
                last_err = f"transport: {e}"
                await _sleep(attempt)
                continue
            if resp.status_code >= 400:
                body_txt = (await resp.aread()).decode("utf-8", "replace")[:300]
                await resp.aclose()
                await client.aclose()
                hard = resp.status_code in (401, 403)
                acc.mark_fail(hard=hard, cooldown=10 if hard else 3)
                last_err = f"status {resp.status_code}: {body_txt}"
                if hard and acc.failures >= 2:
                    pool.recycle(acc, burn=True)
                await _sleep(attempt)
                continue

            acc.mark_ok()
            yield frame({"role": "assistant", "content": ""})

            # ---- producer/consumer so we can emit heartbeats ----
            q: asyncio.Queue = asyncio.Queue(maxsize=2000)
            DONE = object()

            async def reader():
                try:
                    async for chunk in resp.aiter_text():
                        await q.put(chunk)
                except Exception as e:
                    await q.put(("__err__", str(e)))
                finally:
                    await q.put(DONE)

            rt = asyncio.create_task(reader())
            buf = ""
            sid = session_id
            finished = False
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=HEARTBEAT)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"  # keep-alive, prevents idle drops
                        continue
                    if item is DONE:
                        break
                    if isinstance(item, tuple) and item and item[0] == "__err__":
                        break
                    buf += item
                    blocks = buf.split("\n\n")
                    buf = blocks.pop()
                    for b in blocks:
                        ev = _parse_sse_block(b)
                        if not ev:
                            continue
                        t = ev.get("type")
                        if t == "run_start":
                            sid = ev.get("session_id", sid)
                            if sid:
                                yield frame({"twinmind_session": sid})
                        elif t == "text_delta":
                            yield frame({"content": ev.get("content", "")})
                        elif t == "thinking_delta":
                            yield frame({"reasoning_content": ev.get("content", "")})
                        elif t == "done":
                            yield frame({}, finish="stop")
                            finished = True
            finally:
                rt.cancel()
                try:
                    await resp.aclose()
                finally:
                    await client.aclose()
            if not finished:
                yield frame({}, finish="stop")
            _account_done(acc)
            yield "data: [DONE]\n\n"
            return

        err = {"error": {"message": f"All accounts failed. Last: {last_err}", "type": "upstream_error"}}
        yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/completions")
async def completions(request: Request, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    prompt = body.get("prompt")
    if isinstance(prompt, list):
        prompt = prompt[0] if prompt else ""
    if not prompt:
        raise HTTPException(status_code=400, detail="'prompt' is required")
    model = _norm_model(body.get("model"))
    response_id = f"cmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    last_err = "unknown"
    async with httpx.AsyncClient(timeout=REQ_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES):
            acc = pool.acquire()
            try:
                token = acc.ensure_token()
            except Exception as e:
                acc.mark_fail(hard=True, cooldown=30)
                last_err = f"auth: {e}"
                continue
            try:
                resp = await _open_stream(client, token, _build_payload(prompt, model, None))
            except httpx.HTTPError as e:
                acc.mark_fail(cooldown=5)
                last_err = f"transport: {e}"
                await _sleep(attempt)
                continue
            if resp.status_code >= 400:
                await resp.aread()
                await resp.aclose()
                acc.mark_fail(hard=resp.status_code in (401, 403), cooldown=5)
                last_err = f"status {resp.status_code}"
                await _sleep(attempt)
                continue
            acc.mark_ok()
            text_parts: list[str] = []
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                blocks = buf.split("\n\n")
                buf = blocks.pop()
                for b in blocks:
                    ev = _parse_sse_block(b)
                    if ev and ev.get("type") == "text_delta":
                        text_parts.append(ev.get("content", ""))
            await resp.aclose()
            text = "".join(text_parts)
            _account_done(acc)
            return {
                "id": response_id, "object": "text_completion", "created": created,
                "model": model,
                "choices": [{"text": text, "index": 0, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": max(1, len(prompt) // 4),
                    "completion_tokens": max(1, len(text) // 4),
                    "total_tokens": max(1, len(prompt) // 4) + max(1, len(text) // 4),
                },
            }
    raise HTTPException(status_code=502, detail=f"All accounts failed. Last: {last_err}")


if __name__ == "__main__":
    uvicorn.run(app, host=SERVE_HOST, port=SERVE_PORT, log_level="info")
