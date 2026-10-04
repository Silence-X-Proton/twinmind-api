#!/usr/bin/env python3
"""Claude-Code style agent engine.

Two engines, both streamed to the browser as normalized events:

  * claude  -> drives the real Claude Code CLI (`claude -p ... --output-format
    stream-json`) inside the session workspace. Full root, no permission
    prompts (IS_SANDBOX=1 + --dangerously-skip-permissions), resumable sessions.
  * openai  -> talks to any OpenAI-compatible endpoint (the built-in TwinMind
    gateway, or a user-defined custom provider with base_url + api_key + models).

Normalized events emitted downstream:
  {"type":"init",  "session_id", "model", "tools", "cwd"}
  {"type":"text",  "text"}
  {"type":"thinking", "text"}
  {"type":"tool_use", "name", "input", "id"}
  {"type":"tool_result", "content", "is_error"}
  {"type":"file", "path", "action"}          # file touched by Write/Edit
  {"type":"result", "text", "cost_usd", "usage", "session_id", "is_error"}
  {"type":"error", "message"}
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from typing import Any, AsyncGenerator, Optional

CLAUDE_BIN = os.environ.get("TWINMIND_CLAUDE_BIN", "claude")

# Files/commands the agent is allowed to touch. Root, unrestricted, as requested.
CLAUDE_TOOLS = "Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS,WebFetch,WebSearch,Task,NotebookEdit"


def claude_available() -> bool:
    return shutil.which(CLAUDE_BIN) is not None


def _provider_env(provider: Optional[dict]) -> dict:
    """Build env for the Claude Code subprocess.

    A custom provider (kind=anthropic) overrides base_url + key; otherwise we
    inherit ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL from the server environment.
    """
    env = dict(os.environ)
    # Root safe-harbour flag: without it Claude Code refuses
    # --dangerously-skip-permissions when running as root (e.g. in a VPS/container).
    env["IS_SANDBOX"] = "1"
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    if provider:
        base = (provider.get("base_url") or "").strip()
        key = (provider.get("api_key") or "").strip()
        if base:
            # Accept either an Anthropic-style root or an OpenAI-style /v1 root.
            b = base.rstrip("/")
            env["ANTHROPIC_BASE_URL"] = b[:-3] if b.endswith("/v1") else b
        if key:
            env["ANTHROPIC_API_KEY"] = key
            env["ANTHROPIC_AUTH_TOKEN"] = key
    return env


def _norm_content(content: Any) -> list[dict]:
    """Normalize a message.content (str | list) into a list of block dicts."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [c for c in content if isinstance(c, dict)]
    return []


def normalize_cc_event(obj: dict) -> list[dict]:
    """Turn one Claude Code stream-json line into zero or more UI events."""
    out: list[dict] = []
    t = obj.get("type")

    if t == "system":
        if obj.get("subtype") == "init":
            out.append({"type": "init", "session_id": obj.get("session_id", ""),
                        "model": obj.get("model", ""), "tools": obj.get("tools", []),
                        "cwd": obj.get("cwd", ""), "version": obj.get("claude_code_version", "")})
        return out

    if t == "stream_event":
        ev = obj.get("event") or {}
        et = ev.get("type")
        if et == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                out.append({"type": "text", "text": d["text"]})
            elif d.get("type") == "thinking_delta" and d.get("thinking"):
                out.append({"type": "thinking", "text": d["thinking"]})
        return out

    if t == "assistant":
        msg = obj.get("message") or {}
        for c in _norm_content(msg.get("content")):
            ct = c.get("type")
            if ct == "text" and c.get("text"):
                out.append({"type": "text", "text": c["text"]})
            elif ct == "thinking" and c.get("thinking"):
                out.append({"type": "thinking", "text": c["thinking"]})
            elif ct == "tool_use":
                name = c.get("name", "tool")
                inp = c.get("input") or {}
                out.append({"type": "tool_use", "name": name, "input": inp, "id": c.get("id")})
                fp = inp.get("file_path") or inp.get("path")
                if fp and name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                    out.append({"type": "file", "path": fp, "action": name.lower()})
        return out

    if t == "user":
        msg = obj.get("message") or {}
        for c in _norm_content(msg.get("content")):
            if c.get("type") == "tool_result":
                content = c.get("content")
                if isinstance(content, list):
                    content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                out.append({"type": "tool_result", "content": (content or "")[:4000],
                            "is_error": bool(c.get("is_error"))})
        return out

    if t == "result":
        out.append({"type": "result", "text": obj.get("result", "") or "",
                    "cost_usd": obj.get("total_cost_usd"), "usage": obj.get("usage") or {},
                    "session_id": obj.get("session_id", ""),
                    "is_error": bool(obj.get("is_error")),
                    "duration_ms": obj.get("duration_ms")})
        return out

    return out


async def stream_claude(
    prompt: str,
    workspace: str,
    *,
    claude_session_id: str = "",
    model: str = "",
    provider: Optional[dict] = None,
    extra_dirs: Optional[list[str]] = None,
    max_turns: int = 0,
) -> AsyncGenerator[dict, None]:
    """Run Claude Code headless and yield normalized events."""
    if not claude_available():
        yield {"type": "error", "message": "Claude Code CLI not installed. Run: npm install -g @anthropic-ai/claude-code"}
        return

    os.makedirs(workspace, exist_ok=True)
    new_session = not claude_session_id
    sid = claude_session_id or str(uuid.uuid4())

    cmd = [CLAUDE_BIN, "-p", prompt,
           "--output-format", "stream-json",
           "--verbose",
           "--include-partial-messages",
           "--dangerously-skip-permissions",
           "--permission-mode", "bypassPermissions"]
    if new_session:
        cmd += ["--session-id", sid]
    else:
        cmd += ["--resume", sid]
    # Only forward a real model choice; 'default'/'auto'/'' let the CLI decide.
    if model and model.lower() not in ("default", "auto", "none", "cli"):
        cmd += ["--model", model]
    cmd += ["--add-dir", workspace]
    for d in (extra_dirs or []):
        if d and os.path.isdir(d):
            cmd += ["--add-dir", d]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]

    env = _provider_env(provider)

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=workspace, env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )

    saw_result = False
    try:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                yield {"type": "text", "text": s}
                continue
            for ev in normalize_cc_event(obj):
                if ev.get("type") == "result":
                    saw_result = True
                yield ev
    finally:
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()

    if not saw_result:
        err = ""
        if proc.stderr is not None:
            try:
                err = (await asyncio.wait_for(proc.stderr.read(), timeout=2)).decode("utf-8", "replace")
            except Exception:
                err = ""
        if proc.returncode not in (0, None):
            yield {"type": "error", "message": (err or f"claude exited {proc.returncode}")[:2000]}


async def stream_openai(
    messages: list[dict],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 900.0,
) -> AsyncGenerator[dict, None]:
    """Stream from any OpenAI-compatible /chat/completions endpoint."""
    import httpx

    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "messages": messages, "stream": True}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    yield {"type": "error", "message": f"HTTP {resp.status_code}: {body[:1500]}"}
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        o = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if o.get("error"):
                        yield {"type": "error", "message": str(o["error"])[:1500]}
                        continue
                    for ch in o.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            yield {"type": "text", "text": delta["content"]}
                        if delta.get("reasoning_content"):
                            yield {"type": "thinking", "text": delta["reasoning_content"]}
        yield {"type": "result", "text": "", "is_error": False}
    except Exception as e:
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
