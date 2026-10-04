import asyncio
import unittest
from cc_routes import _with_heartbeat

class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_model_survives_multiple_heartbeats(self):
        async def source():
            await asyncio.sleep(.085)
            yield {'type': 'text', 'text': 'SLOW_OK'}
        events = [e async for e in _with_heartbeat(source(), .02)]
        self.assertGreaterEqual(sum(e['type']=='heartbeat' for e in events), 2)
        self.assertEqual(events[-1]['text'], 'SLOW_OK')

    async def test_close_cancels_pending_read(self):
        closed = asyncio.Event()
        async def source():
            try:
                await asyncio.sleep(60)
                yield {}
            finally:
                closed.set()
        stream = _with_heartbeat(source(), .02)
        self.assertEqual((await anext(stream))['type'], 'heartbeat')
        await stream.aclose()
        self.assertTrue(closed.is_set())

    async def test_errors_not_replaced_with_success(self):
        async def source():
            await asyncio.sleep(.025)
            raise RuntimeError('upstream failed')
            yield {}
        with self.assertRaisesRegex(RuntimeError, 'upstream failed'):
            async for _ in _with_heartbeat(source(), .01):
                pass
