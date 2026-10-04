"""Streaming transport helpers. No provider auth, account rotation or tool emulation.

Limits apply to a single SSE frame, not to the total response length. A socket
closing is not equivalent to a provider declaring its response complete.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator


class StreamError(RuntimeError):
    pass


class SSEDecoder:
    def __init__(self, max_frame_chars: int = 2 * 1024 * 1024):
        if max_frame_chars <= 0:
            raise ValueError("max_frame_chars must be positive")
        self.limit = max_frame_chars
        self.buffer = ""
        self.data: list[str] = []
        self.size = 0
        self.pending_cr = False

    def _line(self, line: str) -> list[str]:
        if not line:
            frames = ["\n".join(self.data)] if self.data else []
            self.data.clear()
            self.size = 0
            return frames
        self.size += len(line) + 1
        if self.size > self.limit:
            raise StreamError("Upstream SSE frame exceeds the configured size limit")
        if line.startswith(":"):
            return []
        field, sep, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self.data.append(value if sep else "")
        return []

    def feed(self, text: str) -> list[str]:
        if not text:
            return []
        # Consume exactly one LF after a CR from the previous chunk.
        if self.pending_cr:
            self.pending_cr = False
            if text.startswith("\n"):
                text = text[1:]
        if text.endswith("\r"):
            self.pending_cr = True
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = (self.buffer + normalized).split("\n")
        self.buffer = lines.pop()
        frames: list[str] = []
        for line in lines:
            frames.extend(self._line(line))
        if self.size + len(self.buffer) > self.limit:
            raise StreamError("Upstream SSE frame exceeds the configured size limit")
        return frames

    def finish(self) -> list[str]:
        # Tolerate a complete final JSON event without the customary blank line.
        frames = self._line(self.buffer) if self.buffer else []
        self.buffer = ""
        frames.extend(self._line(""))
        return frames


def json_event(data: str) -> dict:
    if data.strip() == "[DONE]":
        return {"type": "done"}
    try:
        event = json.loads(data)
    except (ValueError, TypeError) as exc:
        raise StreamError("Malformed JSON in upstream SSE event") from exc
    if not isinstance(event, dict):
        raise StreamError("Upstream SSE event must be an object")
    return event


async def iter_sse(chunks: AsyncIterator[str], max_frame_chars: int = 2 * 1024 * 1024):
    decoder = SSEDecoder(max_frame_chars)
    async for chunk in chunks:
        for frame in decoder.feed(chunk):
            yield frame
    for frame in decoder.finish():
        yield frame


async def upstream_events(response, max_frame_chars: int = 2 * 1024 * 1024):
    """Decode a text provider stream, including initial text and reasoning blocks."""
    async for frame in iter_sse(response.aiter_text(), max_frame_chars):
        event = json_event(frame)
        if event.get("error") or event.get("type") in ("error", "run_error"):
            raise StreamError("Upstream reported a generation error")
        yield event
        if event.get("type") == "done":
            return
    raise StreamError("Upstream disconnected before the completion event; response is incomplete")


async def with_heartbeat(source, interval: float = 15, idle_timeout: float = 180):
    """Yield None while waiting. Never cancel a pending read just for a heartbeat.

    Backpressure is one pending event, not an unbounded queue. Idle timeout is
    time without a provider event, not a limit on the full generation duration.
    """
    it = source.__aiter__()
    pending = None
    loop = asyncio.get_running_loop()
    last_event = loop.time()
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
                last_event = loop.time()
            remaining = idle_timeout - (loop.time() - last_event)
            if remaining <= 0:
                raise StreamError("Upstream idle timeout; generation has not completed")
            ready, _ = await asyncio.wait({pending}, timeout=min(max(.01, interval), remaining))
            if not ready:
                yield None
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


class Capture:
    """Keep delivered output for the existing request log, with a bounded preview."""
    def __init__(self, limit: int = 256 * 1024):
        self.limit = limit
        self.parts: list[str] = []
        self.chars = 0
        self.saved = 0

    def append(self, text: str):
        self.chars += len(text)
        room = max(0, self.limit - self.saved)
        if room:
            self.parts.append(text[:room])
            self.saved += min(room, len(text))

    @property
    def text(self):
        return "".join(self.parts)

    @property
    def truncated(self):
        return self.chars > self.saved
