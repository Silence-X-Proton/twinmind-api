import unittest
from unittest.mock import patch
import cc_bridge

class ContextTests(unittest.TestCase):
    def test_latest_long_request_is_not_cut(self):
        latest='START '+('requirements\n'*5000)+' END'
        with patch.object(cc_bridge, 'MAX_CONVO_CHARS', 500):
            output=cc_bridge._render_messages([{'role':'user','content':'old text'},{'role':'user','content':latest}])
        self.assertIn(latest, output)
        self.assertNotIn('old text', output)

    def test_retained_messages_are_complete(self):
        with patch.object(cc_bridge, 'MAX_CONVO_CHARS', 60):
            output=cc_bridge._render_messages([{'role':'user','content':'x'*100},{'role':'assistant','content':'answer'},{'role':'user','content':'next'}])
        self.assertIn('Assistant: answer',output)
        self.assertIn('User: next',output)
        self.assertNotIn('x'*10,output)
