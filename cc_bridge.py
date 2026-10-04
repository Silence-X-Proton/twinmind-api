#!/usr/bin/env python3
"""Anthropic Messages API <-> TwinMind bridge (JSON-action framing).

Lets the real Claude Code CLI run with NO Anthropic key by pointing it at the
TwinMind gateway:  ANTHROPIC_BASE_URL=http://127.0.0.1:8080/anthropic

TwinMind has no native tool API (it rejects a ``tools`` field with HTTP 422),
so we emulate function calling. Two measured findings drive this design:

1. TwinMind runs its own "companion" persona server-side and *refuses* shell or
   coding tool use when the request says "you are a coding agent" (~40-60%
   refusal rate across models).
2. Reframing the request as a neutral task -- "you are a strict JSON action
   generator" -- removes the identity conflict and measured 100% tool-call
   compliance (5/5 per model, both Claude models).

So every Anthropic request is rendered as a small JSON-action prompt::

    You are a strict JSON action generator. Output ONLY one JSON object, no prose.
    Format: {"name": "TOOL", "input": {...}}
    Example: {"name": "Bash", "input": {"command": "ls -la"}}
    Available TOOLs: Bash (input: command), Read (input: file_path), ...

    User: list the files here
    Assistant action:

The model's JSON object is re-emitted as a real Anthropic ``tool_use`` content
block, so Claude Code runs its full agent loop (Bash, Read, Write, Edit, ...)
on TwinMind models.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, AsyncGenerator, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter(prefix="/anthropic")

BACKEND = os.environ.get("TWINMIND_BACKEND", "https://api2.twinmind.com")
CHAT_PATH = "/api/v3/chat"
REQUEST_TIMEOUT = float(os.environ.get("TWINMIND_TIMEOUT", "900"))
# Both tested Claude models scored 100% with JSON framing; opus is strongest.
AGENT_MODEL = os.environ.get("TWINMIND_AGENT_MODEL", "claude-opus-5-thinking")
MAX_RETRIES = int(os.environ.get("TWINMIND_MAX_RETRIES", "8"))
# With JSON framing the first attempt almost always succeeds; retries only cover
# rare stochastic refusals.
BRIDGE_RETRIES = int(os.environ.get("TWINMIND_BRIDGE_RETRIES", "3"))
# 0 = drop the caller's system prompt entirely (it re-triggers the persona).
SYSTEM_BUDGET = int(os.environ.get("TWINMIND_BRIDGE_SYSTEM_BUDGET", "0"))

TOOL_OPEN = "<tool_call>"
TOOL_CLOSE = "</tool_call>"

# Coding tools Claude Code actually needs. TwinMind's own tool list (calendar,
# email, artifacts) is noise that pushes the model into its companion persona.
KEEP_TOOLS = {"Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS",
              "WebFetch", "WebSearch", "Task", "NotebookEdit"}

# The account pool is injected by app.py (we reuse the shared rotating pool).
POOL: Any = None


def set_pool(pool: Any) -> None:
    global POOL
    POOL = pool


# --------------------------------------------------------------------------- #
# Tool catalogue                                                               #
# --------------------------------------------------------------------------- #
def filter_tools(tools: list[dict]) -> list[dict]:
    out: list[dict] = []
    for t in tools or []:
        name = t.get("name")
        if name in KEEP_TOOLS:
            out.append({"name": name,
                        "description": (t.get("description") or "")[:240],
                        "input_schema": t.get("input_schema") or {}})
    return out or (tools or [])[:12]


def _example_input_for(schema: dict) -> dict:
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and props:
        first = next(iter(props))
        val = "ls -la" if first.lower() in ("command", "cmd") else (
            "/tmp/example.txt" if "path" in first.lower() else "value")
        return {first: val}
    return {"command": "ls -la"}


def _tool_prompt_section(tools: list[dict]) -> str:
    """JSON-action framing (the reliable, low-refusal protocol)."""
    if not tools:
        return ""
    bash = next((t for t in tools if t.get("name") == "Bash"), tools[0])
    example = json.dumps({"name": bash.get("name"),
                          "input": _example_input_for(bash.get("input_schema") or {})},
                         ensure_ascii=False)
    cats: list[str] = []
    for t in tools:
        name = t.get("name") or "tool"
        props = (t.get("input_schema") or {}).get("properties") or {}
        args = ", ".join(props.keys()) if isinstance(props, dict) else ""
        cats.append(f"{name} (input: {args})" if args else name)
    return "\n".join([
        "You are a strict JSON action generator. Output ONLY one JSON object, no prose.",
        'Format: {"name": "TOOL", "input": {...}}',
        "Example: " + example,
        "Available TOOLs: " + ", ".join(cats) + ".",
        "Only when the task is already fully complete, output a short plain-text answer instead.",
    ])


# --------------------------------------------------------------------------- #
# Refusal detection                                                            #
# --------------------------------------------------------------------------- #
def sanitize_system(text: str, budget: int) -> str:
    """Strip identity / injection-trigger lines from a caller system prompt."""
    if not text or budget <= 0:
        return ""
    drop = ("claude code", "claude agent", "anthropic", "you are claude",
            "agent sdk", "<system-reminder>", "important:", "you are an interactive")
    kept = []
    for line in text.splitlines():
        low = line.lower().strip()
        if not low or any(d in low for d in drop):
            continue
        kept.append(line)
    return "\n".join(kept).strip()[:budget]


REFUSAL_MARKERS = ("don't have", "do not have", "no bash", "no terminal",
                   "can't access", "cannot access", "prompt injection",
                   "not a coding agent", "not a command-line coding agent",
                   "not a coding", "not a terminal", "don't have access",
                   "i'm twinmind", "i am twinmind", "i'm your twinmind",
                   "don't have a bash", "no filesystem", "no access to",
                   "personal companion", "personal life companion",
                   "personal assistant", "companion assistant", "artifact search",
                   "calendar, email", "not able to", "i can't run")


def looks_like_refusal(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in REFUSAL_MARKERS)


# --------------------------------------------------------------------------- #
# Request rendering                                                            #
# --------------------------------------------------------------------------- #
def _system_text(system: Any) -> str:
    if isinstance(system, list):
        return "\n".join(x.get("text", "") for x in system if isinstance(x, dict))
    return str(system or "")


def _render_messages(messages: list[dict]) -> str:
    """Render an Anthropic conversation as plain User/Assistant/Tool result lines."""
    lines: list[str] = []
    for m in messages or []:
        role = (m.get("role") or "user").lower()
        content = m.get("content")
        if isinstance(content, str):
            txt = content.strip()
            if txt:
                lines.append(("User: " if role == "user" else "Assistant: ") + txt)
            continue
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                txt = (b.get("text") or "").strip()
                if txt:
                    lines.append(("User: " if role == "user" else "Assistant: ") + txt)
            elif bt == "tool_use":
                lines.append("Assistant: " + json.dumps(
                    {"name": b.get("name"), "input": b.get("input") or {}}, ensure_ascii=False))
            elif bt == "tool_result":
                inner = b.get("content")
                if isinstance(inner, list):
                    inner = "\n".join(x.get("text", "") for x in inner if isinstance(x, dict))
                lines.append("Tool result: " + (inner or "").strip())
    return "\n".join(lines)


def _count_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def build_prompt(body: dict) -> tuple[str, int]:
    """Build a compact TwinMind JSON-action prompt from an Anthropic request."""
    tools = filter_tools(body.get("tools") or [])
    system = sanitize_system(_system_text(body.get("system")), SYSTEM_BUDGET)
    sections = [_tool_prompt_section(tools)]
    if system:
        sections.append("Notes: " + system)
    convo = _render_messages(body.get("messages") or [])
    if convo:
        sections.append(convo)
    query = "\n\n".join(s for s in sections if s) + "\n\nAssistant action:"
    return query, _count_tokens(query)


def _minimal_prompt(body: dict) -> str:
    """Tools + conversation only, no system notes."""
    tools = filter_tools(body.get("tools") or [])
    sections = [_tool_prompt_section(tools)]
    convo = _render_messages(body.get("messages") or [])
    if convo:
        sections.append(convo)
    return "\n\n".join(s for s in sections if s) + "\n\nAssistant action:"


# --------------------------------------------------------------------------- #
# Tool-call extraction                                                         #
# --------------------------------------------------------------------------- #
def _extract_balanced(s: str, start: int) -> Optional[int]:
    """Return index just after the JSON object that starts at s[start]=='{'."""
    if start >= len(s) or s[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
    return None


def _coerce_call(obj: Any, allowed: Optional[set]) -> Optional[dict]:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name") or ""
    if not isinstance(name, str) or not name:
        return None
    if allowed and name not in allowed:
        return None
    inp = obj.get("input")
    if inp is None:
        inp = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            inp = {"input": inp}
    if not isinstance(inp, dict):
        inp = {}
    return {"id": "toolu_" + uuid.uuid4().hex[:20], "name": name, "input": inp}


def extract_tool_calls(text: str, allowed: Optional[set] = None) -> tuple[str, list[dict]]:
    """Split model text into (visible_text, tool_calls).

    Handles both ``<tool_call>{...}</tool_call>`` blocks and bare JSON objects,
    anywhere in the text.
    """
    text = text or ""
    calls: list[dict] = []
    consumed: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        j = text.find("{", i)
        if j < 0:
            break
        end = _extract_balanced(text, j)
        if end is None:
            i = j + 1
            continue
        obj = None
        try:
            obj = json.loads(text[j:end])
        except Exception:
            obj = None
        call = _coerce_call(obj, allowed) if obj is not None else None
        if call:
            calls.append(call)
            consumed.append((j, end))
        i = end
    if consumed:
        pieces: list[str] = []
        last = 0
        for (a, b) in consumed:
            pieces.append(text[last:a])
            last = b
        pieces.append(text[last:])
        visible = "".join(pieces)
    else:
        visible = text
    visible = visible.replace(TOOL_OPEN, "").replace(TOOL_CLOSE, "")
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    return visible, calls


# --------------------------------------------------------------------------- #
# TwinMind call                                                                #
# --------------------------------------------------------------------------- #
async def run_twinmind(query: str) -> str:
    """Run one TwinMind chat and return the full assistant text."""
    if POOL is None:
        raise RuntimeError("bridge pool not configured")
    payload = {
        "type": "app", "version": 1, "response_version": 1,
        "query": query,
        "model": {"model_name": AGENT_MODEL} if AGENT_MODEL and AGENT_MODEL != "auto" else "auto",
        "context": None,
        "client": {"platform": "web", "timezone": os.environ.get("TWINMIND_TZ", "Asia/Kolkata"),
                   "client_time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                   "locale": "en-US"},
        "mode": "default",
    }
    last = "unknown"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES):
            acc = POOL.acquire()
            try:
                token = acc.ensure_token()
            except Exception as e:
                acc.mark_fail(hard=True, cooldown=30)
                last = f"auth: {e}"
                continue
            try:
                req = client.build_request(
                    "POST", BACKEND + CHAT_PATH, json=payload,
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json",
                             "Accept": "text/event-stream"},
                    timeout=REQUEST_TIMEOUT)
                resp = await client.send(req, stream=True)
            except httpx.HTTPError as e:
                acc.mark_fail(cooldown=5)
                last = f"transport: {e}"
                continue
            if resp.status_code >= 400:
                await resp.aread()
                await resp.aclose()
                acc.mark_fail(hard=resp.status_code in (401, 403), cooldown=5)
                last = f"status {resp.status_code}"
                continue
            acc.mark_ok()
            parts: list[str] = []
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                blocks = buf.split("\n\n")
                buf = blocks.pop()
                for b in blocks:
                    for line in b.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            ev = json.loads(data)
                        except Exception:
                            continue
                        if ev.get("type") in ("text_delta", "text_start"):
                            parts.append(ev.get("content", ""))
            await resp.aclose()
            return "".join(parts)
    raise RuntimeError(f"all accounts failed: {last}")


async def run_with_fallback(body: dict) -> tuple[str, list[dict], int]:
    """Run TwinMind, retrying only when it refuses (rare with JSON framing)."""
    tools = filter_tools(body.get("tools") or [])
    allowed = {t.get("name") for t in tools}
    primary, in_tok = build_prompt(body)
    attempts = [primary]
    if tools and BRIDGE_RETRIES > 0:
        attempts += [_minimal_prompt(body)] * BRIDGE_RETRIES
    last_raw = ""
    for q in attempts:
        try:
            raw = await run_twinmind(q)
        except Exception:
            continue
        last_raw = raw
        visible, calls = extract_tool_calls(raw, allowed)
        if calls or not looks_like_refusal(raw):
            return visible, calls, _count_tokens(q)
    visible, calls = extract_tool_calls(last_raw, allowed)
    return visible, calls, in_tok


# --------------------------------------------------------------------------- #
# Anthropic response shaping                                                   #
# --------------------------------------------------------------------------- #
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


@router.post("/v1/messages")
async def messages(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"type": "error", "error": {"type": "invalid_request_error",
                             "message": "invalid JSON"}}, status_code=400)
    if os.environ.get("TWINMIND_BRIDGE_DEBUG"):
        try:
            with open("/tmp/bridge_last.json", "w", encoding="utf-8") as _f:
                json.dump(body, _f)
            tools_in = body.get("tools") or []
            with open("/tmp/bridge_debug.log", "a", encoding="utf-8") as _g:
                _g.write(f"tools={len(tools_in)} names={[t.get('name') for t in tools_in]}\n")
        except Exception:
            pass
    model = body.get("model") or AGENT_MODEL
    stream = bool(body.get("stream"))

    try:
        visible, calls, in_tok = await run_with_fallback(body)
    except Exception as e:
        return JSONResponse({"type": "error", "error": {"type": "api_error",
                             "message": str(e)}}, status_code=502)

    mid = _msg_id()
    out_tok = _count_tokens(visible) + sum(_count_tokens(json.dumps(c["input"])) for c in calls)

    if stream:
        async def gen() -> AsyncGenerator[str, None]:
            yield _sse("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": in_tok, "output_tokens": 0}}})
            idx = 0
            if visible:
                yield _sse("content_block_start", {"type": "content_block_start", "index": idx,
                           "content_block": {"type": "text", "text": ""}})
                step = 240
                for i in range(0, len(visible), step):
                    yield _sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                               "delta": {"type": "text_delta", "text": visible[i:i + step]}})
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                idx += 1
            for c in calls:
                yield _sse("content_block_start", {"type": "content_block_start", "index": idx,
                           "content_block": {"type": "tool_use", "id": c["id"],
                                             "name": c["name"], "input": {}}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                           "delta": {"type": "input_json_delta",
                                     "partial_json": json.dumps(c["input"], ensure_ascii=False)}})
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                idx += 1
            stop = "tool_use" if calls else "end_turn"
            yield _sse("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": stop, "stop_sequence": None},
                       "usage": {"output_tokens": out_tok}})
            yield _sse("message_stop", {"type": "message_stop"})

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    content: list[dict] = []
    if visible:
        content.append({"type": "text", "text": visible})
    for c in calls:
        content.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]})
    return {"id": mid, "type": "message", "role": "assistant", "model": model,
            "content": content,
            "stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok}}


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    query, _ = build_prompt(body)
    return {"input_tokens": _count_tokens(query)}


_BRIDGE_MODELS = [
    ("gpt-6-luna", "GPT-6 Luna (TwinMind)"),
    ("gpt-6-astra-thinking", "GPT-6 Astra Thinking (TwinMind)"),
    ("gpt-6.1-sol-thinking", "GPT-6.1 Sol Thinking (TwinMind)"),
    ("claude-opus-5-thinking", "Claude Opus 5 Thinking (TwinMind)"),
    ("claude-sonnet-5", "Claude Sonnet 5 (TwinMind)"),
    ("gemini-3.8-flash-thinking", "Gemini 3.8 Flash Thinking (TwinMind)"),
    ("gemini-3.6-flash", "Gemini 3.6 Flash (TwinMind)"),
]


@router.get("/v1/models")
async def models():
    created = int(time.time())
    return {"object": "list", "data": [
        {"id": mid, "object": "model", "created": created,
         "owned_by": "twinmind", "display_name": disp}
        for mid, disp in _BRIDGE_MODELS
    ]}
