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
import signal
import uuid
from typing import Any, AsyncGenerator, Optional

from gateway_stream import StreamError, iter_sse, json_event

CLAUDE_BIN = os.environ.get("TWINMIND_CLAUDE_BIN", "claude")

# Files/commands the agent is allowed to touch. Root, unrestricted, as requested.
CLAUDE_TOOLS = "Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS,WebFetch,WebSearch,Task,NotebookEdit"


def claude_available() -> bool:
    return shutil.which(CLAUDE_BIN) is not None


def _provider_env(provider: Optional[dict], model: str = "") -> dict:
    """Build env for the Claude Code subprocess.

    Priority:
      1. explicit custom provider (Anthropic-compatible) -> its base_url + key
      2. real ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL from the server env
      3. NO KEY fallback -> the built-in TwinMind bridge, so Claude Code runs
         on TwinMind models with zero configuration.
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
            b = base.rstrip("/")
            env["ANTHROPIC_BASE_URL"] = b[:-3] if b.endswith("/v1") else b
        if key:
            env["ANTHROPIC_API_KEY"] = key
            env["ANTHROPIC_AUTH_TOKEN"] = key
        return env
    if not env.get("ANTHROPIC_API_KEY") and not env.get("ANTHROPIC_AUTH_TOKEN"):
        port = os.environ.get("TWINMIND_PORT", "8080")
        env["ANTHROPIC_BASE_URL"] = os.environ.get(
            "TWINMIND_BRIDGE_URL", f"http://127.0.0.1:{port}/anthropic")
        env["ANTHROPIC_API_KEY"] = env.get("TWINMIND_BRIDGE_KEY", "twinmind-bridge")
        env["ANTHROPIC_AUTH_TOKEN"] = env["ANTHROPIC_API_KEY"]
        env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] = "1"
        # Tell the bridge which TwinMind model to use for THIS session. Claude
        # Code forwards ANTHROPIC_CUSTOM_HEADERS on every request, so switching
        # the model in the UI takes effect on the next message.
        if model:
            env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-twinmind-model: {model}"
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


# Bound individual JSON records, not StreamReader's much smaller default line
# limit. Large tool outputs are legitimate, but an endless line is not.
_CC_MAX_LINE_BYTES = 16 * 1024 * 1024
_CC_CHUNK_BYTES = 64 * 1024
_CC_STDERR_TAIL_BYTES = 64 * 1024
_CC_TERMINATE_TIMEOUT = 1.0


async def _cc_lines(reader: asyncio.StreamReader) -> AsyncGenerator[bytes, None]:
    pending = bytearray()
    while chunk := await reader.read(_CC_CHUNK_BYTES):
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            stop = len(chunk) if end < 0 else end
            if len(pending) + stop - start > _CC_MAX_LINE_BYTES:
                raise ValueError(f"Claude stdout JSON line exceeds {_CC_MAX_LINE_BYTES} bytes")
            pending.extend(chunk[start:stop])
            if end < 0:
                break
            yield bytes(pending)
            pending.clear()
            start = end + 1
    if pending:
        yield bytes(pending)


class _CCStreamEvents:
    """Reconcile append-only partials with snapshots of the same message/block.

    Never deduplicate by global text: a later message can legitimately repeat
    the same words. Anonymous snapshots consume their pending partial state.
    """

    def __init__(self):
        self.message_id = None
        self.blocks: dict[tuple[int, str], str] = {}
        self.saw_text = False

    def normalize(self, obj: dict) -> list[dict]:
        out: list[dict] = []
        kind = obj.get("type")
        if kind == "stream_event":
            event = obj.get("event") or {}
            if event.get("type") == "message_start":
                self.message_id = (event.get("message") or {}).get("id")
                self.blocks.clear()
            out = normalize_cc_event(obj)
            if event.get("type") == "content_block_start":
                block = event.get("content_block") or {}
                block_kind = block.get("type")
                if block_kind in ("text", "thinking") and block.get(block_kind):
                    out = [{"type": block_kind, "text": block[block_kind]}]
            for item in out:
                key = (event.get("index", 0), item["type"])
                self.blocks[key] = self.blocks.get(key, "") + item["text"]
        elif kind == "assistant":
            message = obj.get("message") or {}
            mid = message.get("id")
            if mid is not None and self.message_id is not None and mid != self.message_id:
                self.blocks.clear()
            self.message_id = mid
            for index, block in enumerate(_norm_content(message.get("content"))):
                block_kind = block.get("type")
                if block_kind in ("text", "thinking"):
                    text = block.get(block_kind) or ""
                    key = (index, block_kind)
                    partial = self.blocks.get(key, "")
                    # Snapshots are normally equal to or extend the partial.
                    # A stale shorter snapshot must not replay existing text.
                    if text.startswith(partial):
                        suffix = text[len(partial):]
                    elif partial.startswith(text):
                        suffix = ""
                    else:
                        # The UI cannot retract a divergent partial. Preserve
                        # the changed snapshot rather than silently lose it.
                        suffix = text
                    if suffix:
                        out.append({"type": block_kind, "text": suffix})
                        self.blocks[key] = partial + suffix
                else:
                    out.extend(normalize_cc_event({"type": "assistant", "message": {"content": [block]}}))
            if mid is None:
                self.blocks.clear()
        else:
            out = normalize_cc_event(obj)
            if kind == "result" and not self.saw_text and obj.get("result"):
                # Both the route's saved transcript and the UI consume text,
                # not result.text. Keep the metadata event as well.
                out.insert(0, {"type": "text", "text": obj["result"]})
        self.saw_text |= any(e["type"] == "text" and e.get("text") for e in out)
        return out


async def _cc_feed(writer: asyncio.StreamWriter, prompt: str) -> None:
    try:
        # Encode slices so neither argv limits nor a second full UTF-8 copy of
        # an arbitrarily long prompt constrains input size.
        for start in range(0, len(prompt), _CC_CHUNK_BYTES):
            writer.write(prompt[start:start + _CC_CHUNK_BYTES].encode("utf-8"))
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # A CLI may reject a request before consuming its entire input.
    finally:
        writer.close()


async def _cc_drain(reader: asyncio.StreamReader, tail: Optional[bytearray] = None) -> None:
    while chunk := await reader.read(_CC_CHUNK_BYTES):
        if tail is not None:
            tail.extend(chunk)
            del tail[:-_CC_STDERR_TAIL_BYTES]


async def _cc_cleanup(spawn: asyncio.Task, tasks: list[asyncio.Task]) -> None:
    # Shielded by the owner, including when cancellation races process spawn.
    try:
        proc = await spawn
    except Exception:
        return

    def send(sig):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            elif proc.returncode is None:
                proc.send_signal(sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    # Stop blocked prompt writes; abort drops buffered input on early close.
    if tasks:
        tasks[0].cancel()
    if proc.stdin is not None:
        transport = proc.stdin.transport
        if not transport.is_closing() or transport.get_write_buffer_size():
            transport.abort()
    if proc.stdout is not None:
        tasks.append(asyncio.create_task(_cc_drain(proc.stdout)))
    if not tasks or len(tasks) == 1:
        if proc.stderr is not None:
            tasks.append(asyncio.create_task(_cc_drain(proc.stderr)))
    try:
        deadline = asyncio.get_running_loop().time() + _CC_TERMINATE_TIMEOUT
        while True:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
            elif proc.returncode is not None:
                break
            if asyncio.get_running_loop().time() >= deadline:
                send(signal.SIGKILL)
                break
            await asyncio.sleep(.02)
        await proc.wait()  # Reap even after escalation, never fire-and-forget.
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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

    cmd = [CLAUDE_BIN, "-p",
           "--output-format", "stream-json",
           "--verbose",
           "--include-partial-messages",
           "--dangerously-skip-permissions",
           "--permission-mode", "bypassPermissions"]
    if new_session:
        cmd += ["--session-id", sid]
    else:
        cmd += ["--resume", sid]
    # --model only accepts Anthropic aliases (opus/sonnet/haiku) or full Anthropic
    # names. TwinMind ids are routed to the bridge via the x-twinmind-model header
    # instead, so the CLI's own model label stays valid.
    if model and model.lower() in ("opus", "sonnet", "haiku"):
        cmd += ["--model", model]
    cmd += ["--add-dir", workspace]
    for d in (extra_dirs or []):
        if d and os.path.isdir(d):
            cmd += ["--add-dir", d]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]

    env = _provider_env(provider, model if (env_uses_bridge := (not provider)) else "")

    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *cmd, cwd=workspace, env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    ))
    tasks: list[asyncio.Task] = []
    tail = bytearray()
    events = _CCStreamEvents()
    transport_error = None
    try:
        proc = await asyncio.shield(spawn)
        assert proc.stdout is not None and proc.stderr is not None and proc.stdin is not None
        tasks.append(asyncio.create_task(_cc_feed(proc.stdin, prompt)))
        tasks.append(asyncio.create_task(_cc_drain(proc.stderr, tail)))
        async for line in _cc_lines(proc.stdout):
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                events.saw_text = True
                yield {"type": "text", "text": s}
                continue
            if isinstance(obj, dict):
                for ev in events.normalize(obj):
                    yield ev
        await proc.wait()
        await asyncio.gather(*tasks)
    except (OSError, ValueError) as exc:
        transport_error = str(exc)
    finally:
        cleanup = asyncio.create_task(_cc_cleanup(spawn, tasks))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    if transport_error:
        yield {"type": "error", "message": transport_error}
    elif proc.returncode not in (0, None):
        err = tail.decode("utf-8", "replace")[-2000:]
        yield {"type": "error", "message": err or f"claude exited {proc.returncode}"}


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
                complete = False
                finish_reason = None
                async for data in iter_sse(resp.aiter_text()):
                    if data.strip() == "[DONE]":
                        complete = True
                        break
                    event = json_event(data)
                    if event.get("error"):
                        yield {"type": "error", "message": "Provider stream failed; partial output was preserved"}
                        return
                    if event.get("usage"):
                        yield {"type": "usage", "usage": event["usage"],
                               "estimated": bool(event.get("usage_estimated"))}
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            yield {"type": "text", "text": delta["content"]}
                        if delta.get("reasoning_content"):
                            yield {"type": "thinking", "text": delta["reasoning_content"]}
                        if delta.get("tool_calls"):
                            # Transport only: never execute a partial argument object.
                            yield {"type": "tool_call_delta", "tool_calls": delta["tool_calls"]}
                        if choice.get("finish_reason") is not None:
                            finish_reason = choice["finish_reason"]
                if not complete:
                    raise StreamError("Provider disconnected before [DONE]; response is incomplete")
        yield {"type": "result", "text": "", "is_error": False,
               "finish_reason": finish_reason, "truncated": finish_reason == "length"}
    except Exception as e:
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
