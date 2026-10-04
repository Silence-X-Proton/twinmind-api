import asyncio
import json
import unittest
from unittest.mock import patch
from starlette.requests import Request
import cc_bridge

class BridgeLivenessTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        async def receive():
            return {'type':'http.request','body':json.dumps({'stream':True,'messages':[]}).encode()}
        return Request({'type':'http','method':'POST','path':'/','headers':[]},receive)
    async def test_ping_precedes_delayed_generation(self):
        ready=asyncio.Event()
        async def provider(*args):
            await ready.wait()
            return 'reply',[],1
        with patch.object(cc_bridge,'run_with_fallback',side_effect=provider):
            response=await cc_bridge.messages(self.request())
            stream=response.body_iterator
            first=await asyncio.wait_for(anext(stream),.1)
            self.assertIn('event: ping',first)
            ready.set()
            rest=''.join([x async for x in stream])
            self.assertIn('reply',rest)
            self.assertIn('message_stop',rest)
    async def test_close_cancels_provider(self):
        closed=asyncio.Event()
        async def provider(*args):
            try: await asyncio.sleep(60)
            finally: closed.set()
        with patch.object(cc_bridge,'run_with_fallback',side_effect=provider):
            response=await cc_bridge.messages(self.request())
            stream=response.body_iterator
            await anext(stream)
            await asyncio.sleep(0)
            await stream.aclose()
            self.assertTrue(closed.is_set())
    async def test_empty_output_is_an_error_not_success(self):
        async def provider(*args): return '',[],0
        with patch.object(cc_bridge,'run_with_fallback',side_effect=provider):
            response=await cc_bridge.messages(self.request())
            text=''.join([x async for x in response.body_iterator])
            self.assertIn('event: error',text)
            self.assertNotIn('message_stop',text)
