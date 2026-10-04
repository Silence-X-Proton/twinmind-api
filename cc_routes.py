#!/usr/bin/env python3
"""HTTP + SSE API for the Claude-Code style agent UI (mounted under /agent)."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import cc_agent
import cc_store as store

router = APIRouter(prefix="/agent")

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY_BASE = os.environ.get("TWINMIND_GATEWAY_BASE", "http://127.0.0.1:{port}/v1")
MAX_UPLOAD = int(os.environ.get("TWINMIND_MAX_UPLOAD", str(25 * 1024 * 1024)))
HEARTBEAT = float(os.environ.get("TWINMIND_AGENT_HEARTBEAT", "10"))
MAX_TURNS = int(os.environ.get("TWINMIND_AGENT_MAX_TURNS", "30"))


def _gateway_base() -> str:
    port = os.environ.get("TWINMIND_PORT", "8080")
    return GATEWAY_BASE.format(port=port)


def _gateway_key() -> str:
    keys = os.environ.get("TWINMIND_API_KEYS", "")
    return keys.split(",")[0].strip() if keys else ""


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


async def _with_heartbeat(agen, hb: float = 10.0):
    """Keep one pending read alive across heartbeat intervals; cancel only on close."""
    it = agen.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
            ready, _ = await asyncio.wait({pending}, timeout=max(.01, hb))
            if not ready:
                yield {"type": "heartbeat"}
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield event
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(it, "aclose", None)
        if close is not None:
            await close()


# --------------------------------------------------------------------------- #
# UI                                                                           #
# --------------------------------------------------------------------------- #
@router.get("")
async def agent_ui():
    path = os.path.join(HERE, "agent.html")
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"error": "agent.html not found"}, status_code=404)


@router.get("/api/status")
async def status():
    return {
        "claude_installed": cc_agent.claude_available(),
        "claude_bin": cc_agent.CLAUDE_BIN,
        "gateway_base": _gateway_base(),
        "anthropic_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "providers": len(store.list_providers()),
    }


# --------------------------------------------------------------------------- #
# Sessions                                                                     #
# --------------------------------------------------------------------------- #
@router.get("/api/sessions")
async def list_sessions():
    return {"sessions": store.list_sessions()}


@router.post("/api/sessions")
async def create_session(payload: Optional[dict] = None):
    payload = payload or {}
    meta = store.create_session(
        title=payload.get("title") or "New chat",
        engine=payload.get("engine") or "claude",
        model=payload.get("model") or "",
        provider_id=payload.get("provider_id") or "",
    )
    return meta


@router.get("/api/sessions/{sid}")
async def get_session(sid: str):
    meta = store.get_session(sid)
    if not meta:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session": meta, "messages": store.get_messages(sid)}


@router.patch("/api/sessions/{sid}")
async def patch_session(sid: str, payload: dict):
    allowed = {k: v for k, v in (payload or {}).items()
               if k in ("title", "engine", "model", "provider_id", "claude_session_id")}
    meta = store.update_session(sid, **allowed)
    if not meta:
        raise HTTPException(status_code=404, detail="session not found")
    return meta


@router.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    return {"deleted": store.delete_session(sid)}


# --------------------------------------------------------------------------- #
# Files / workspace                                                            #
# --------------------------------------------------------------------------- #
@router.get("/api/sessions/{sid}/files")
async def list_files(sid: str):
    if not store.get_session(sid):
        raise HTTPException(status_code=404, detail="session not found")
    return {"files": store.list_files(sid), "workspace": store.workspace_dir(sid)}


@router.get("/api/sessions/{sid}/file")
async def read_file(sid: str, path: str):
    r = store.read_file(sid, path)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r.get("error", "not found"))
    return r


@router.post("/api/sessions/{sid}/file")
async def write_file(sid: str, payload: dict):
    if not store.get_session(sid):
        raise HTTPException(status_code=404, detail="session not found")
    try:
        return store.write_file(sid, payload.get("path") or "", payload.get("content") or "",
                                overwrite=payload.get("overwrite") is True)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")


@router.delete("/api/sessions/{sid}/file")
async def delete_file(sid: str, path: str):
    return {"deleted": store.delete_file(sid, path)}


@router.post("/api/sessions/{sid}/upload")
async def upload(sid: str, file: UploadFile = File(...)):
    if not store.get_session(sid):
        raise HTTPException(status_code=404, detail="session not found")
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail=f"file too large (max {MAX_UPLOAD} bytes)")
    return store.save_upload(sid, file.filename or "upload.bin", data)


@router.get("/api/sessions/{sid}/search")
async def search_session(sid: str, q: str = ""):
    if not store.get_session(sid):
        raise HTTPException(status_code=404, detail="session not found")
    return store.search(sid, q)


@router.get("/api/search")
async def search_all(q: str = ""):
    results = []
    for s in store.list_sessions():
        r = store.search(s["id"], q, limit=10)
        if r["messages"] or r["files"]:
            results.append({"session": s, "messages": r["messages"], "files": r["files"]})
    return {"results": results}


# --------------------------------------------------------------------------- #
# Providers                                                                    #
# --------------------------------------------------------------------------- #
@router.get("/api/providers")
async def list_providers():
    return {"providers": [store.mask_provider(p) for p in store.list_providers()]}


@router.post("/api/providers")
async def add_provider(payload: dict):
    models = payload.get("models") or []
    if isinstance(models, str):
        models = [m.strip() for m in models.replace(",", "\n").splitlines()]
    prov = store.add_provider(
        name=payload.get("name") or "Provider",
        base_url=payload.get("base_url") or "",
        api_key=payload.get("api_key") or "",
        models=models,
        kind=payload.get("kind") or "openai",
    )
    return store.mask_provider(prov)


@router.patch("/api/providers/{pid}")
async def patch_provider(pid: str, payload: dict):
    fields = {}
    for k in ("name", "base_url", "api_key", "models", "kind"):
        if k in (payload or {}):
            fields[k] = payload[k]
    if isinstance(fields.get("models"), str):
        fields["models"] = [m.strip() for m in fields["models"].replace(",", "\n").splitlines() if m.strip()]
    if not fields.get("api_key"):
        fields.pop("api_key", None)  # keep existing key when blank
    p = store.update_provider(pid, **fields)
    if not p:
        raise HTTPException(status_code=404, detail="provider not found")
    return store.mask_provider(p)


@router.delete("/api/providers/{pid}")
async def remove_provider(pid: str):
    return {"deleted": store.delete_provider(pid)}


# --------------------------------------------------------------------------- #
# Models catalog (twinmind gateway + custom providers)                         #
# --------------------------------------------------------------------------- #
@router.get("/api/models")
async def models():
    out: list[dict] = []
    try:
        import httpx
        headers = {}
        key = _gateway_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(_gateway_base() + "/models", headers=headers)
            if r.status_code < 400:
                for m in (r.json() or {}).get("data", []):
                    out.append({"id": m.get("id"), "group": "TwinMind", "owner": m.get("owned_by")})
    except Exception:
        pass
    for p in store.list_providers():
        for m in p.get("models", []):
            out.append({"id": m, "group": p.get("name") or "Custom", "owner": p.get("id"),
                        "provider_id": p.get("id"), "kind": p.get("kind")})
    return {"models": out}


# --------------------------------------------------------------------------- #
# Chat (SSE)                                                                   #
# --------------------------------------------------------------------------- #
@router.post("/api/sessions/{sid}/chat")
async def chat(sid: str, request: Request):
    meta = store.get_session(sid)
    if not meta:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    message = (body.get("message") or "").strip()
    attachments = body.get("attachments") or []
    engine = body.get("engine") or meta.get("engine") or "claude"
    model = body.get("model") or meta.get("model") or ""
    provider_id = body.get("provider_id") or meta.get("provider_id") or ""
    if not message and not attachments:
        raise HTTPException(status_code=400, detail="empty message")

    store.update_session(sid, engine=engine, model=model, provider_id=provider_id)
    att_note = ""
    if attachments:
        att_note = "\n\n[Attached files in workspace: " + ", ".join(attachments) + "]"
    user_content = message + att_note
    store.add_message(sid, "user", user_content, {"attachments": attachments})
    if meta.get("message_count", 0) == 0 and not meta.get("title_locked"):
        store.update_session(sid, title=(message or "New chat")[:60], title_locked=True)

    workspace = store.workspace_dir(sid)
    provider = store.get_provider(provider_id) if provider_id else None

    async def gen():
        yield _sse({"type": "start", "engine": engine, "model": model})
        final_text: list[str] = []
        trace: list[dict] = []
        new_cc_sid = ""
        is_error = False
        try:
            if engine == "claude":
                prompt = user_content
                async for ev in _with_heartbeat(cc_agent.stream_claude(
                    prompt, workspace,
                    claude_session_id=meta.get("claude_session_id") or "",
                    model=model, provider=provider,
                    max_turns=MAX_TURNS,
                ), HEARTBEAT):
                    if await request.is_disconnected():
                        break
                    t = ev.get("type")
                    if t == "init":
                        new_cc_sid = ev.get("session_id") or new_cc_sid
                    elif t == "text":
                        final_text.append(ev.get("text", ""))
                    elif t in ("tool_use", "tool_result", "file"):
                        trace.append(ev)
                    elif t == "result":
                        if ev.get("session_id"):
                            new_cc_sid = ev["session_id"]
                        is_error = bool(ev.get("is_error"))
                    elif t == "error":
                        is_error = True
                    yield _sse(ev)
            else:
                # OpenAI-compatible engine: TwinMind gateway or a custom provider.
                if provider:
                    base_url = provider.get("base_url") or ""
                    api_key = provider.get("api_key") or ""
                else:
                    base_url = _gateway_base()
                    api_key = _gateway_key()
                if not base_url:
                    yield _sse({"type": "error", "message": "No base_url configured for this provider"})
                    is_error = True
                else:
                    history = [{"role": m["role"], "content": m["content"]}
                               for m in store.get_messages(sid) if m.get("role") in ("user", "assistant")]
                    async for ev in _with_heartbeat(cc_agent.stream_openai(
                        history, base_url=base_url, api_key=api_key,
                        model=model or "auto",
                    ), HEARTBEAT):
                        if await request.is_disconnected():
                            break
                        if ev.get("type") == "text":
                            final_text.append(ev.get("text", ""))
                        elif ev.get("type") == "error":
                            is_error = True
                        yield _sse(ev)
        except asyncio.CancelledError:
            is_error = True
            raise
        except Exception as e:
            is_error = True
            final_text.append(f"Error: {type(e).__name__}: {e}")
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            text = "".join(final_text)
            store.add_message(sid, "assistant", text, {"trace": trace, "is_error": is_error,
                                                        "engine": engine, "model": model})
            if new_cc_sid and new_cc_sid != meta.get("claude_session_id"):
                store.update_session(sid, claude_session_id=new_cc_sid)
        yield _sse({"type": "done", "is_error": is_error})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
