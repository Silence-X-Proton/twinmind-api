"""Offline Claude transport tests; no real CLI, provider or application data."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import cc_agent as agent


class ClaudeStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cc-agent-test-")
        self.root = Path(self.temp.name)
        self.executable = self.root / "fake-claude"
        self.processes = []
        self.generators = []
        self.spawn = asyncio.create_subprocess_exec

        async def record_spawn(*args, **kwargs):
            proc = await self.spawn(*args, **kwargs)
            self.processes.append(proc)
            return proc

        self.patches = [
            mock.patch.object(agent, "CLAUDE_BIN", str(self.executable)),
            mock.patch.object(agent, "_provider_env", return_value={"PATH": "/usr/bin:/bin"}),
            mock.patch.object(agent.asyncio, "create_subprocess_exec", side_effect=record_spawn),
        ]
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self):
        # Emergency cleanup also makes intentionally failing pre-fix runs safe.
        for proc in self.processes:
            try:
                if os.getpgid(proc.pid) == proc.pid:
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        await asyncio.wait_for(stream.read(), 2)
                    except (asyncio.TimeoutError, RuntimeError):
                        pass
            await asyncio.wait_for(proc.wait(), 3)
        for gen in self.generators:
            await gen.aclose()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def fake(self, body):
        self.executable.write_text(
            f"#!{sys.executable}\n"
            "import hashlib, json, os, signal, subprocess, sys, time\n"
            "def emit(obj):\n"
            "    print(json.dumps(obj), flush=True)\n" + textwrap.dedent(body)
        )
        self.executable.chmod(0o700)

    def stream(self, prompt="test", **kwargs):
        gen = agent.stream_claude(prompt, str(self.root), **kwargs)
        self.generators.append(gen)
        return gen

    async def collect(self, prompt="test", timeout=4, **kwargs):
        async def consume():
            return [event async for event in self.stream(prompt, **kwargs)]
        return await asyncio.wait_for(consume(), timeout)

    @staticmethod
    def text(events, kind="text"):
        return "".join(e["text"] for e in events if e["type"] == kind)

    async def test_delayed_output_is_streamed_before_exit(self):
        self.fake('''
            time.sleep(.15)
            emit({"type": "assistant", "message": {"content": "first"}})
            time.sleep(.6)
            emit({"type": "result", "result": "first"})
        ''')
        gen = self.stream()
        first = await asyncio.wait_for(anext(gen), 2)
        self.assertEqual(first, {"type": "text", "text": "first"})
        self.assertIsNone(self.processes[0].returncode)
        rest = [e async for e in gen]
        self.assertEqual(self.text(rest), "")
        self.assertEqual(rest[-1]["type"], "result")

    async def test_stdout_larger_than_default_readline_limit(self):
        self.fake('''
            emit({"type": "assistant", "message": {"content": "é" * 100000}})
        ''')
        events = await self.collect()
        self.assertEqual(self.text(events), "é" * 100000)

    async def test_large_stderr_before_stdout_and_bounded_error_tail(self):
        self.fake('''
            sys.stderr.write("discard-me" + "x" * 2000000 + "TAIL-MARKER")
            sys.stderr.flush()
            emit({"type": "assistant", "message": {"content": "ready"}})
            sys.exit(7)
        ''')
        events = await self.collect()
        self.assertEqual(self.text(events), "ready")
        errors = [e["message"] for e in events if e["type"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("TAIL-MARKER", errors[0])
        self.assertNotIn("discard-me", errors[0])
        self.assertLessEqual(len(errors[0]), 2200)

    async def test_large_prompt_stdin_and_concurrent_output(self):
        self.fake('''
            assert sys.argv[1:4] == ["-p", "--output-format", "stream-json"]
            # Both output pipes fill BEFORE this process starts reading stdin.
            sys.stderr.write("e" * 500000)
            sys.stderr.flush()
            for i in range(50):
                emit({"type": "system", "subtype": "ignored", "padding": "x" * 10000})
            prompt = sys.stdin.buffer.read()
            emit({"type": "assistant", "message": {"content": hashlib.sha256(prompt).hexdigest()}})
        ''')
        prompt = "long é prompt\n" * 100000
        events = await self.collect(prompt, timeout=6)
        self.assertEqual(self.text(events), hashlib.sha256(prompt.encode()).hexdigest())

    async def test_partial_snapshots_scoped_to_message_and_block(self):
        self.fake('''
            for mid in ("m1", "m2"):
                emit({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
                for index, kind, value in ((0, "thinking", "reason"), (1, "text", "same"), (2, "text", "same")):
                    emit({"type": "stream_event", "event": {"type": "content_block_start", "index": index, "content_block": {"type": kind, kind: ""}}})
                    emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": index, "delta": {"type": kind + "_delta", kind: value}}})
                emit({"type": "stream_event", "event": {"type": "message_stop"}})
                snapshot = {"type": "assistant", "message": {"id": mid, "content": [
                    {"type": "thinking", "thinking": "reason"},
                    {"type": "text", "text": "same"},
                    {"type": "text", "text": "same!"},
                    {"type": "tool_use", "name": "Write", "id": mid, "input": {"file_path": "example.txt"}}
                ]}}
                emit(snapshot)
                emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
            emit({"type": "result", "result": "same!", "usage": {"output_tokens": 7}, "total_cost_usd": .1})
        ''')
        events = await self.collect()
        self.assertEqual(self.text(events), "samesame!samesame!")
        self.assertEqual(self.text(events, "thinking"), "reasonreason")
        for kind in ("tool_use", "file", "tool_result"):
            self.assertEqual(sum(e["type"] == kind for e in events), 2)
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["usage"], {"output_tokens": 7})
        self.assertEqual(events[-1]["cost_usd"], .1)

    async def test_result_only_displays_once_and_preserves_result(self):
        self.fake('''
            emit({"type": "result", "result": "result only", "session_id": "fake", "is_error": False})
        ''')
        events = await self.collect()
        self.assertEqual([e["type"] for e in events], ["text", "result"])
        self.assertEqual(self.text(events), "result only")
        self.assertEqual(events[-1]["text"], "result only")
        self.assertEqual(events[-1]["session_id"], "fake")

    async def test_nonzero_even_after_result(self):
        self.fake('''
            emit({"type": "result", "result": "answer"})
            sys.stderr.write("fatal-after-result")
            sys.exit(9)
        ''')
        events = await self.collect()
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("fatal-after-result", events[-1]["message"])

    async def test_nonzero_without_stderr(self):
        self.fake("sys.exit(3)")
        events = await self.collect()
        self.assertEqual(events, [{"type": "error", "message": "claude exited 3"}])

    async def test_unterminated_line_and_plaintext(self):
        self.fake('''
            print("plain output", flush=True)
            sys.stdout.write(json.dumps({"type": "assistant", "message": {"content": "tail"}}))
        ''')
        self.assertEqual(self.text(await self.collect()), "plain outputtail")

    def persistent_fake(self):
        self.fake('''
            # Parent ignores TERM to force escalation, but reaps its own child.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGCHLD, signal.SIG_IGN)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            with open("child.pid", "w") as f:
                f.write(str(child.pid))
            emit({"type": "assistant", "message": {"content": "ready"}})
            time.sleep(60)
        ''')

    def assert_reaped(self):
        proc = self.processes[0]
        self.assertIsNotNone(proc.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(proc.pid, 0)
        child = int((self.root / "child.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    async def test_generator_close_kills_group_and_reaps(self):
        self.persistent_fake()
        gen = self.stream("p" * 1000000)
        await asyncio.wait_for(anext(gen), 3)
        started = time.monotonic()
        await asyncio.wait_for(gen.aclose(), 3)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assert_reaped()

    async def test_cancellation_kills_group_and_reaps(self):
        self.persistent_fake()
        gen = self.stream()
        await asyncio.wait_for(anext(gen), 3)
        pending = asyncio.create_task(anext(gen))
        await asyncio.sleep(.05)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        self.assert_reaped()


if __name__ == "__main__":
    unittest.main()
