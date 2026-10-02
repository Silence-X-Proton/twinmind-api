#!/usr/bin/env python3
"""
TwinMind -> OpenAI-compatible API server + Admin Dashboard (premium build).

Public (OpenAI-compatible):
  GET  /v1/models
  GET  /v1/models/{id}
  POST /v1/chat/completions        (stream + non-stream)
  POST /v1/completions             (legacy)
  GET  /health

Admin UI + API (served at /admin):
  GET  /admin                      -> dashboard (mobile friendly)
  GET  /admin/api/stats            -> totals, per-model, pool, gateway state
  GET  /admin/api/requests         -> recent request log
  GET  /admin/api/requests/{id}    -> full request detail (prompt + output + tokens)
  POST /admin/api/summarize        -> summarize text with a chosen model
  POST /admin/api/gateway          -> turn the gateway ON/OFF
  POST /admin/api/pool/resize      -> grow/shrink the account pool
  POST /admin/api/pool/add         -> add one account
  POST /admin/api/pool/rotate      -> burn oldest + replace
  POST /admin/api/pool/burn-all    -> burn every account + refill
  POST /admin/api/terminal         -> run a shell command (admin)

Reliability / "never hit a limit":
  * rotating AccountPool + automatic retry on any 401/403/429/5xx
  * circuit-broken + auto-replaced accounts
  * BURN-AFTER-USE: after TWINMIND_REQ_PER_ACCOUNT requests an account is
    deleted upstream (user + Firebase) and replaced -> no trace.
Premium streaming: SSE with heartbeat keep-alives (no mid-stream break).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Iterable, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from accounts import AccountPool

# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.environ.get("TWINMIND_BACKEND", "https://api2.twinmind.com")
CHAT_PATH = "/api/v3/chat"
MODELS_PATH = "/api/v3/chat/models"

POOL_SIZE = int(os.environ.get("TWINMIND_POOL_SIZE", "15"))
STATE_FILE = os.environ.get("TWINMIND_STATE_FILE", os.path.join(HERE, "pool_state.json"))
MAX_RETRIES = int(os.environ.get("TWINMIND_MAX_RETRIES", "8"))
REQ_TIMEOUT = float(os.environ.get("TWINMIND_TIMEOUT", "900"))
SERVE_HOST = os.environ.get("TWINMIND_HOST", "0.0.0.0")
SERVE_PORT = int(os.environ.get("TWINMIND_PORT", "8080"))
API_KEYS = {k for k in os.environ.get("TWINMIND_API_KEYS", "").split(",") if k}
# Optional admin password. Empty = open access to /admin.
ADMIN_KEY = os.environ.get("TWINMIND_ADMIN_KEY", "")
DEFAULT_MODEL = os.environ.get("TWINMIND_DEFAULT_MODEL", "auto")

REQ_PER_ACCOUNT = int(os.environ.get("TWINMIND_REQ_PER_ACCOUNT", "300"))
ROTATE_INTERVAL = int(os.environ.get("TWINMIND_ROTATE_INTERVAL", "0"))
HEARTBEAT = float(os.environ.get("TWINMIND_HEARTBEAT", "15"))
REQUEST_LOG_SIZE = int(os.environ.get("TWINMIND_REQUEST_LOG", "1000"))

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
# Runtime state (stats + request log + gateway toggle)                         #
# --------------------------------------------------------------------------- #
class State:
    def __init__(self) -> None:
        self.started = time.time()
        self.gateway_enabled = True
        self.total_requests = 0
        self.total_in_tokens = 0
        self.total_out_tokens = 0
        self.failed_requests = 0
        self.models: dict[str, dict] = {}
        self.requests: deque = deque(maxlen=REQUEST_LOG_SIZE)
        self._by_id: dict[str, dict] = {}

    def record(self, entry: dict) -> None:
        self.requests.appendleft(entry)
        self._by_id[entry["id"]] = entry
        if len(self._by_id) > REQUEST_LOG_SIZE * 2:
            keep = {e["id"] for e in self.requests}
            self._by_id = {k: v for k, v in self._by_id.items() if k in keep}

    def get(self, rid: str) -> Optional[dict]:
        return self._by_id.get(rid)

    def bump_model(self, model: str, in_tok: int, out_tok: int, ok: bool) -> None:
        m = self.models.setdefault(model, {"requests": 0, "in_tokens": 0, "out_tokens": 0, "failed": 0})
        m["requests"] += 1
        m["in_tokens"] += in_tok
        m["out_tokens"] += out_tok
        if not ok:
            m["failed"] += 1


STATE = State()


def _tok(s: str) -> int:
    return max(0, len(s or "") // 4)


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


app = FastAPI(title="TwinMind OpenAI-Compatible API", version="3.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
_TRANSIENT_STATUS = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


def _norm_model(model: str | None) -> str:
    if not model:
        return DEFAULT_MODEL
    m = model.strip()
    aliases = {
        "gpt-4": "gpt-6-luna", "gpt-4o": "gpt-6-luna", "gpt-4o-mini": "gpt-6-luna",
        "gpt-3.5-turbo": "gemini-3.6-flash", "gpt-5": "gpt-6-luna",
        "o1": "gpt-6-astra-thinking", "claude-3": "claude-sonnet-5",
        "claude-3-opus": "claude-opus-5-thinking", "claude-3-sonnet": "claude-sonnet-5",
        "claude-3-haiku": "claude-sonnet-5", "gemini": "gemini-3.6-flash",
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


def _check_admin(key: Optional[str]) -> None:
    if not ADMIN_KEY:
        return
    if (key or "").strip() != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")


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
    if REQ_PER_ACCOUNT and acc.total_requests >= REQ_PER_ACCOUNT:
        try:
            asyncio.get_running_loop().create_task(asyncio.to_thread(pool.burn_and_replace, acc))
        except RuntimeError:
            pass


async def _run_chat(query: str, model: str, session_id: Optional[str]) -> tuple[str, str, Optional[str]]:
    """Internal non-streaming chat used by admin (summarize etc.).
    Returns (text, thinking, session_id)."""
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
                await resp.aread()
                await resp.aclose()
                acc.mark_fail(hard=resp.status_code in (401, 403), cooldown=5)
                last_err = f"status {resp.status_code}"
                await _sleep(attempt)
                continue
            acc.mark_ok()
            text_parts: list[str] = []
            think_parts: list[str] = []
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
                        think_parts.append(ev.get("content", ""))
            await resp.aclose()
            _account_done(acc)
            return "".join(text_parts), "".join(think_parts), sid
    raise RuntimeError(f"All accounts failed. Last: {last_err}")


# --------------------------------------------------------------------------- #
# Public routes                                                                #
# --------------------------------------------------------------------------- #
@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin")


@app.get("/health")
async def health():
    s = pool.stats()
    return {
        "status": "ok",
        "gateway": "on" if STATE.gateway_enabled else "off",
        "pool": {"size": s["size"], "available": s["available"]},
    }


@app.get("/v1/models")
async def list_models(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    created = int(time.time())
    async with httpx.AsyncClient(timeout=REQ_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES):
            acc = pool.acquire()
            try:
                token = acc.ensure_token()
            except Exception as e:
                acc.mark_fail(hard=True, cooldown=30)
                continue
            try:
                resp = await client.get(BACKEND + MODELS_PATH, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError:
                acc.mark_fail(cooldown=5)
                await _sleep(attempt)
                continue
            if resp.status_code >= 400:
                acc.mark_fail(hard=resp.status_code in (401, 403), cooldown=5)
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
    data = []
    for prov_id, _n, mods in STATIC_CATALOG:
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
    if not STATE.gateway_enabled:
        raise HTTPException(status_code=503, detail="Gateway is OFF (disabled by admin)")
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
    rid = f"req_{uuid.uuid4().hex[:16]}"
    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    started = time.time()
    in_tok = _tok(query)

    def finalize(text: str, thinking: str, sid: Optional[str], ok: bool) -> None:
        out_tok = _tok(text)
        STATE.total_requests += 1
        STATE.total_in_tokens += in_tok
        STATE.total_out_tokens += out_tok
        if not ok:
            STATE.failed_requests += 1
        STATE.bump_model(model, in_tok, out_tok, ok)
        STATE.record({
            "id": rid, "ts": started, "model": model, "stream": stream,
            "endpoint": "/v1/chat/completions", "status": "ok" if ok else "error",
            "in_tokens": in_tok, "out_tokens": out_tok,
            "prompt": query, "output": text,
            "thinking": thinking, "session": sid,
            "duration": round(time.time() - started, 3),
        })

    if not stream:
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
                    await resp.aread()
                    await resp.aclose()
                    hard = resp.status_code in (401, 403)
                    acc.mark_fail(hard=hard, cooldown=10 if hard else 3)
                    last_err = f"status {resp.status_code}"
                    if hard and acc.failures >= 2:
                        pool.recycle(acc, burn=True)
                    await _sleep(attempt)
                    continue
                acc.mark_ok()
                text_parts: list[str] = []
                think_parts: list[str] = []
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
                            think_parts.append(ev.get("content", ""))
                await resp.aclose()
                text = "".join(text_parts)
                thinking = "".join(think_parts)
                _account_done(acc)
                finalize(text, thinking, sid, True)
                msg = {"role": "assistant", "content": text}
                if thinking:
                    msg["reasoning_content"] = thinking
                if sid:
                    msg["twinmind_session"] = sid
                return {
                    "id": response_id, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": in_tok, "completion_tokens": _tok(text),
                        "total_tokens": in_tok + _tok(text),
                    },
                }
        finalize("", "", session_id, False)
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
                await resp.aread()
                await resp.aclose()
                await client.aclose()
                hard = resp.status_code in (401, 403)
                acc.mark_fail(hard=hard, cooldown=10 if hard else 3)
                last_err = f"status {resp.status_code}"
                if hard and acc.failures >= 2:
                    pool.recycle(acc, burn=True)
                await _sleep(attempt)
                continue

            acc.mark_ok()
            yield frame({"role": "assistant", "content": ""})

            q: asyncio.Queue = asyncio.Queue(maxsize=4000)
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
            full_text: list[str] = []
            full_think: list[str] = []
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=HEARTBEAT)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
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
                            full_text.append(ev.get("content", ""))
                            yield frame({"content": ev.get("content", "")})
                        elif t == "thinking_delta":
                            full_think.append(ev.get("content", ""))
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
            finalize("".join(full_text), "".join(full_think), sid, True)
            yield "data: [DONE]\n\n"
            return

        finalize("", "", session_id, False)
        err = {"error": {"message": f"All accounts failed. Last: {last_err}", "type": "upstream_error"}}
        yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/completions")
async def completions(request: Request, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if not STATE.gateway_enabled:
        raise HTTPException(status_code=503, detail="Gateway is OFF (disabled by admin)")
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
    rid = f"req_{uuid.uuid4().hex[:16]}"
    created = int(time.time())
    started = time.time()
    in_tok = _tok(prompt)
    text, think, sid = await _run_chat(prompt, model, None)
    out_tok = _tok(text)
    STATE.total_requests += 1
    STATE.total_in_tokens += in_tok
    STATE.total_out_tokens += out_tok
    STATE.bump_model(model, in_tok, out_tok, True)
    STATE.record({
        "id": rid, "ts": started, "model": model, "stream": False,
        "endpoint": "/v1/completions", "status": "ok",
        "in_tokens": in_tok, "out_tokens": out_tok,
        "prompt": prompt, "output": text, "thinking": think, "session": sid,
        "duration": round(time.time() - started, 3),
    })
    return {
        "id": f"cmpl-{uuid.uuid4().hex}", "object": "text_completion", "created": created,
        "model": model,
        "choices": [{"text": text, "index": 0, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok, "total_tokens": in_tok + out_tok},
    }


# --------------------------------------------------------------------------- #
# Admin dashboard + API                                                        #
# --------------------------------------------------------------------------- #
@app.get("/admin")
async def admin_page():
    path = os.path.join(HERE, "admin.html")
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"error": "admin.html not found"}, status_code=404)


@app.get("/admin/api/stats")
async def admin_stats(key: Optional[str] = None):
    _check_admin(key)
    s = pool.stats()
    return {
        "gateway": STATE.gateway_enabled,
        "uptime": round(time.time() - STATE.started, 1),
        "total_requests": STATE.total_requests,
        "total_in_tokens": STATE.total_in_tokens,
        "total_out_tokens": STATE.total_out_tokens,
        "failed_requests": STATE.failed_requests,
        "pool": {"size": s["size"], "available": s["available"]},
        "models": STATE.models,
        "req_per_account": REQ_PER_ACCOUNT,
        "accounts": s["accounts"],
    }


@app.get("/admin/api/requests")
async def admin_requests(key: Optional[str] = None, limit: int = 100):
    _check_admin(key)
    out = []
    for e in list(STATE.requests)[: max(1, min(limit, 500))]:
        out.append({k: e[k] for k in ("id", "ts", "model", "stream", "endpoint",
                                      "status", "in_tokens", "out_tokens", "duration", "session")})
    return {"requests": out, "count": len(out)}


@app.get("/admin/api/requests/{rid}")
async def admin_request_detail(rid: str, key: Optional[str] = None):
    _check_admin(key)
    e = STATE.get(rid)
    if not e:
        raise HTTPException(status_code=404, detail="Request not found")
    return e


@app.post("/admin/api/summarize")
async def admin_summarize(payload: dict, key: Optional[str] = None):
    _check_admin(key)
    model = _norm_model(payload.get("model") or DEFAULT_MODEL)
    text = payload.get("text") or ""
    rid = payload.get("request_id")
    if rid and not text:
        e = STATE.get(rid)
        if e:
            text = f"User input:\n{e['prompt']}\n\nModel output:\n{e['output']}"
    if not text.strip():
        raise HTTPException(status_code=400, detail="Provide 'text' or a valid 'request_id'")
    prompt = (
        "Summarize the following conversation/request concisely. Give a short overview, "
        "the key points of the user's input, and the essential points of the answer. "
        "Use clear bullet points.\n\n---\n"
        + text[:12000]
    )
    started = time.time()
    in_tok = _tok(prompt)
    summary, _think, _sid = await _run_chat(prompt, model, None)
    out_tok = _tok(summary)
    STATE.total_requests += 1
    STATE.total_in_tokens += in_tok
    STATE.total_out_tokens += out_tok
    STATE.bump_model(model, in_tok, out_tok, True)
    STATE.record({
        "id": f"req_{uuid.uuid4().hex[:16]}", "ts": started, "model": model,
        "stream": False, "endpoint": "/admin/summarize", "status": "ok",
        "in_tokens": in_tok, "out_tokens": out_tok,
        "prompt": prompt, "output": summary, "thinking": "", "session": _sid,
        "duration": round(time.time() - started, 3),
    })
    return {"model": model, "summary": summary, "in_tokens": in_tok, "out_tokens": out_tok}


@app.post("/admin/api/gateway")
async def admin_gateway(payload: dict, key: Optional[str] = None):
    _check_admin(key)
    STATE.gateway_enabled = bool(payload.get("enabled", True))
    return {"gateway": STATE.gateway_enabled}


@app.post("/admin/api/pool/resize")
async def admin_pool_resize(payload: dict, key: Optional[str] = None):
    _check_admin(key)
    size = int(payload.get("size", pool.stats()["size"]))
    res = await asyncio.to_thread(pool.resize, size)
    return res


@app.post("/admin/api/pool/add")
async def admin_pool_add(key: Optional[str] = None):
    _check_admin(key)
    return await asyncio.to_thread(pool.add_account)


@app.post("/admin/api/pool/rotate")
async def admin_pool_rotate(key: Optional[str] = None):
    _check_admin(key)
    nxt = await asyncio.to_thread(pool.rotate)
    return {"rotated": bool(nxt)}


@app.post("/admin/api/pool/burn-all")
async def admin_pool_burn_all(key: Optional[str] = None):
    _check_admin(key)
    burned = 0
    for a in list(pool._accounts):
        try:
            await asyncio.to_thread(pool.burn_and_replace, a)
            burned += 1
        except Exception:
            pass
    return {"burned": burned, "pool_size": pool.stats()["size"]}


@app.post("/admin/api/terminal")
async def admin_terminal(payload: dict, key: Optional[str] = None):
    _check_admin(key)
    cmd = (payload.get("cmd") or "").strip()
    cwd = payload.get("cwd") or HERE
    if not cmd:
        raise HTTPException(status_code=400, detail="'cmd' is required")
    timeout = float(payload.get("timeout", 30))
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            out, _ = await proc.communicate()
            return {"cmd": cmd, "cwd": cwd, "exit": None, "output": (out or b"").decode("utf-8", "replace") + "\n[timeout]", "timeout": True}
        return {"cmd": cmd, "cwd": cwd, "exit": proc.returncode, "output": (out or b"").decode("utf-8", "replace")}
    except Exception as e:
        return {"cmd": cmd, "cwd": cwd, "exit": None, "output": f"error: {e}"}


@app.post("/admin/api/destroy")
async def admin_destroy(payload: dict, key: Optional[str] = None):
    """Full shutdown: turn gateway off, burn all accounts, kill server + tunnel.
    Requires {"confirm": "DESTROY"} in the body (UI asks 4 times)."""
    _check_admin(key)
    if (payload.get("confirm") or "").strip().upper() != "DESTROY":
        raise HTTPException(status_code=400, detail="Confirmation token required")
    STATE.gateway_enabled = False

    async def _shutdown():
        await asyncio.sleep(1.5)
        # burn all accounts (best effort)
        for a in list(pool._accounts):
            try:
                await asyncio.to_thread(a.burn)
            except Exception:
                pass
        # kill tunnel + self
        for pattern in ("cloudflared tunnel", "cloudflared"):
            try:
                p = await asyncio.create_subprocess_shell(f"pkill -f '{pattern}'")
                await p.wait()
            except Exception:
                pass
        os._exit(0)

    asyncio.get_running_loop().create_task(_shutdown())
    return {"destroyed": True, "message": "Shutting down: burning accounts, killing tunnel + server."}


if __name__ == "__main__":
    uvicorn.run(app, host=SERVE_HOST, port=SERVE_PORT, log_level="info")
