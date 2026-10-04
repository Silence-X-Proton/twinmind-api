import asyncio
import json
import unittest
from gateway_stream import Capture, SSEDecoder, StreamError, iter_sse, upstream_events, with_heartbeat

class Response:
    def __init__(self, chunks):
        self.chunks = chunks
    async def aiter_text(self):
        for chunk in self.chunks:
            yield chunk

def wire(event, ending='\n\n'):
    return 'data: '+json.dumps(event, ensure_ascii=False)+ending

class DecoderTests(unittest.TestCase):
    def test_every_single_character_boundary_including_crlf(self):
        text=': comment\r\ndata: {\r\ndata: "type": "text_start", "content": "नमस्ते"}\r\n\r\n'
        decoder=SSEDecoder()
        frames=[]
        for ch in text:
            frames += decoder.feed(ch)
        frames += decoder.finish()
        self.assertEqual(json.loads(frames[0])['content'], 'नमस्ते')
        self.assertEqual(len(frames),1)
    def test_final_unterminated_frame(self):
        decoder=SSEDecoder()
        self.assertEqual(decoder.feed('data: {"type":"done"}'), [])
        self.assertEqual(decoder.finish(), ['{"type":"done"}'])
    def test_oversized_frame_not_unbounded(self):
        with self.assertRaises(StreamError):
            SSEDecoder(64).feed('data: '+('x'*65))
    def test_bounded_capture_does_not_limit_output(self):
        c=Capture(10)
        c.append('a'*8);c.append('b'*8)
        self.assertEqual(c.text, 'a'*8+'bb')
        self.assertEqual(c.chars,16)
        self.assertTrue(c.truncated)

class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_and_final_events_preserved(self):
        text=wire({'type':'text_start','content':'FIRST'})+wire({'type':'text_delta','content':'LAST'})+wire({'type':'done'},'')
        events=[e async for e in upstream_events(Response(list(text)))]
        self.assertEqual(''.join(e.get('content','') for e in events),'FIRSTLAST')
        self.assertEqual(events[-1]['type'],'done')
    async def test_many_megabytes_no_total_frame_limit(self):
        async def chunks():
            yield wire({'type':'text_start','content':'BEGIN'})
            for _ in range(12000):
                yield wire({'type':'text_delta','content':'x'*1024})
            yield wire({'type':'done'})
        class LargeResponse:
            aiter_text=staticmethod(chunks)
        count=0
        async for event in upstream_events(LargeResponse()):
            count+=len(event.get('content',''))
        self.assertEqual(count,5+12000*1024)
    async def test_midstream_disconnect_preserves_partial_then_errors(self):
        seen=[]
        with self.assertRaisesRegex(StreamError,'incomplete'):
            async for event in upstream_events(Response([wire({'type':'text_delta','content':'partial'})])):
                seen.append(event)
        self.assertEqual(seen[0]['content'],'partial')
    async def test_malformed_json_is_not_silently_dropped(self):
        with self.assertRaisesRegex(StreamError,'Malformed'):
            async for _ in upstream_events(Response(['data: {bad}\n\n'])): pass
    async def test_provider_error_propagates(self):
        with self.assertRaisesRegex(StreamError,'generation error'):
            async for _ in upstream_events(Response([wire({'type':'error','message':'failed'})])): pass
    async def test_heartbeat_does_not_cancel_slow_read(self):
        async def source():
            await asyncio.sleep(.075)
            yield 'arrived'
        events=[e async for e in with_heartbeat(source(),.02,.5)]
        self.assertGreaterEqual(events.count(None),2)
        self.assertEqual(events[-1],'arrived')
    async def test_idle_timeout_closes_source(self):
        closed=asyncio.Event()
        async def source():
            try:
                await asyncio.sleep(60)
                yield {}
            finally: closed.set()
        with self.assertRaisesRegex(StreamError,'idle timeout'):
            async for _ in with_heartbeat(source(),.01,.04): pass
        self.assertTrue(closed.is_set())
    async def test_close_cancels_pending_task(self):
        closed=asyncio.Event()
        async def source():
            try:
                await asyncio.sleep(60)
                yield {}
            finally: closed.set()
        stream=with_heartbeat(source(),.01,60)
        await anext(stream)
        await stream.aclose()
        self.assertTrue(closed.is_set())
