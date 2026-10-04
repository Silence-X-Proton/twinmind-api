import json
import unittest
from unittest.mock import patch
import httpx
import cc_agent

class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks): self.chunks=chunks
    async def __aiter__(self):
        for chunk in self.chunks: yield chunk

def event(obj): return ('data: '+json.dumps(obj)+'\r\n\r\n').encode()

class OpenAITransportTests(unittest.IsolatedAsyncioTestCase):
    async def consume(self, chunks):
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=Bytes(chunks))))
        with patch('httpx.AsyncClient',return_value=client):
            return [e async for e in cc_agent.stream_openai([{'role':'user','content':'hi'}],base_url='https://offline/v1',api_key='',model='test')]

    async def test_stream_error_never_followed_by_success(self):
        events=await self.consume([event({'choices':[{'delta':{'content':'partial'}}]}),event({'error':{'message':'failed'}}),b'data: [DONE]\n\n'])
        self.assertEqual(events[0]['text'],'partial')
        self.assertEqual(events[-1]['type'],'error')
        self.assertFalse(any(e['type']=='result' for e in events))

    async def test_eof_is_error(self):
        events=await self.consume([event({'choices':[{'delta':{'content':'partial'}}]})])
        self.assertEqual(events[-1]['type'],'error')
        self.assertIn('incomplete',events[-1]['message'])

    async def test_tool_arguments_preserved_across_fragments(self):
        fragments=[{'index':0,'id':'call_1','type':'function','function':{'name':'Read','arguments':'{"pa'}}, {'index':0,'function':{'arguments':'th":"x"}'}}]
        events=await self.consume([event({'choices':[{'delta':{'tool_calls':[f]}}]}) for f in fragments]+[event({'choices':[{'delta':{},'finish_reason':'tool_calls'}]}),b'data: [DONE]'])
        self.assertEqual([e['tool_calls'][0] for e in events if e['type']=='tool_call_delta'],fragments)
        self.assertEqual(events[-1]['finish_reason'],'tool_calls')

    async def test_provider_length_limit_and_usage_exposed(self):
        events=await self.consume([event({'choices':[{'delta':{'content':'x'},'finish_reason':'length'}]}),event({'choices':[],'usage':{'total_tokens':4}}),b'data: [DONE]\n\n'])
        self.assertTrue(events[-1]['truncated'])
        self.assertEqual(events[-2]['usage']['total_tokens'],4)
