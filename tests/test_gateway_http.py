"""Run actual gateway HTTP routes with a fake pool and fake provider stream."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import httpx
from gateway_stream import SSEDecoder


def frame(kind, content=None, ending='\n\n'):
    event={'type':kind}
    if content is not None:
        event['content']=content
    return 'data: '+json.dumps(event,ensure_ascii=False)+ending


class FakeAccount:
    def ensure_token(self): return 'offline-test-token'
    def mark_ok(self): pass
    def mark_fail(self, **kwargs): pass


class FakePool:
    def __init__(self, **kwargs): pass
    def acquire(self): return FakeAccount()


class Response:
    status_code=200
    def __init__(self, chunks, delay=0, fail=False):
        self.chunks=chunks
        self.delay=delay
        self.fail=fail
        self.closed=False
    async def aiter_text(self):
        for chunk in self.chunks:
            if self.delay: await asyncio.sleep(self.delay)
            yield chunk
        if self.fail: raise httpx.ReadError('connection lost')
    async def aclose(self): self.closed=True


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Import-time pool creation is replaced: tests must never create accounts.
        accounts=types.ModuleType('accounts')
        accounts.AccountPool=FakePool
        spec=importlib.util.spec_from_file_location('gateway_test_app',Path(__file__).parents[1]/'app.py')
        cls.module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'accounts':accounts}):
            spec.loader.exec_module(cls.module)

    async def asyncSetUp(self):
        self.app=self.module
        self.app.STATE=self.app.State()
        self.app.API_KEYS=set()
        self.app.REQ_PER_ACCOUNT=0
        self.app.HEARTBEAT=.01
        self.app.STREAM_IDLE_TIMEOUT=.5
        self.app.STREAM_LOG_CHARS=100
        self.open_count=0
        self.response=None
        async def open_response(*args):
            self.open_count+=1
            return self.response
        self.patcher=patch.object(self.app,'_open_stream',side_effect=open_response)
        self.patcher.start()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app.app),base_url='http://offline')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.patcher.stop()

    async def send(self,stream=True,**extra):
        return await self.client.post('/v1/chat/completions',json={
            'model':'auto','stream':stream,'messages':[{'role':'user','content':'hello'}],**extra})

    @staticmethod
    def events(response):
        decoder=SSEDecoder()
        return [json.loads(x) if x!='[DONE]' else x for x in decoder.feed(response.text)+decoder.finish()]

    async def test_first_fragment_usage_and_unterminated_done(self):
        raw=frame('text_start','FIRST','\r\n\r\n')+frame('text_delta','LAST')+frame('done',ending='')
        self.response=Response(list(raw))
        response=await self.send(stream_options={'include_usage':True})
        events=self.events(response)
        text=''.join(c['delta'].get('content','') for e in events if isinstance(e,dict) for c in e.get('choices',[]))
        self.assertEqual(text,'FIRSTLAST')
        self.assertEqual(events[-1],'[DONE]')
        self.assertTrue(events[-2]['usage_estimated'])
        self.assertTrue(self.response.closed)
        self.assertEqual(self.app.STATE.failed_requests,0)

    async def test_disconnect_is_error_never_stop_or_replay(self):
        self.response=Response([frame('text_start','partial')],fail=True)
        events=self.events(await self.send())
        errors=[e['error'] for e in events if isinstance(e,dict) and 'error' in e]
        self.assertTrue(errors[0]['partial'])
        self.assertFalse(any(c.get('finish_reason')=='stop' for e in events if isinstance(e,dict) for c in e.get('choices',[])))
        self.assertEqual(self.open_count,1)
        self.assertEqual(self.app.STATE.failed_requests,1)
        self.assertTrue(self.response.closed)

    async def test_eof_without_done_is_error(self):
        self.response=Response([frame('text_start','partial')])
        events=self.events(await self.send())
        self.assertTrue(any(isinstance(e,dict) and 'error' in e for e in events))
        self.assertEqual(self.app.STATE.requests[0]['status'],'error')

    async def test_nonstream_initial_text_preserved(self):
        self.response=Response([frame('text_start','G'),frame('text_delta','ATEWAY_OK'),frame('done')])
        response=await self.send(False)
        self.assertEqual(response.json()['choices'][0]['message']['content'],'GATEWAY_OK')
        self.assertTrue(self.response.closed)

    async def test_nonstream_incomplete_returns_502(self):
        self.response=Response([frame('text_start','partial')])
        response=await self.send(False)
        self.assertEqual(response.status_code,502)
        self.assertEqual(self.app.STATE.failed_requests,1)
        self.assertTrue(self.response.closed)

    async def test_large_output_delivered_but_request_log_bounded(self):
        self.response=Response([frame('text_delta','x'*4096)]*300+[frame('done')])
        events=self.events(await self.send())
        chars=sum(len(c['delta'].get('content','')) for e in events if isinstance(e,dict) for c in e.get('choices',[]))
        self.assertEqual(chars,300*4096)
        log=self.app.STATE.requests[0]
        self.assertEqual(len(log['output']),100)
        self.assertTrue(log['log_truncated'])
        self.assertEqual(log['out_tokens'],chars//4)

    async def test_delayed_provider_has_heartbeats_and_finishes(self):
        self.response=Response([frame('text_start','slow'),frame('done')],delay=.035)
        response=await self.send()
        self.assertGreaterEqual(response.text.count(': ping'),2)
        self.assertIn('slow',response.text)
        self.assertEqual(self.app.STATE.failed_requests,0)

    async def test_idle_timeout_does_not_wait_forever(self):
        self.app.STREAM_IDLE_TIMEOUT=.04
        self.response=Response([frame('done')],delay=10)
        response=await asyncio.wait_for(self.send(),3)
        self.assertIn('idle timeout',response.text)
        self.assertEqual(self.app.STATE.failed_requests,1)
        self.assertTrue(self.response.closed)

    async def test_empty_done_is_not_reported_as_success(self):
        self.response=Response([frame('done')])
        events=self.events(await self.send())
        self.assertTrue(any(isinstance(e,dict) and 'error' in e for e in events))
        self.assertEqual(self.app.STATE.failed_requests,1)

    async def test_unsupported_native_tools_explicit_error(self):
        response=await self.send(tools=[{'type':'function','function':{'name':'Read'}}])
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.open_count,0)
