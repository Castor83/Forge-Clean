import json
import unittest

from src.forge_runtime.conversation_protocol import CONVERSATION_SCHEMA
from src.forge_runtime.lm_studio_executor import LMStudioChatClient


class ConversationTransportTests(unittest.TestCase):
    def test_conversation_uses_proposal_schema_and_no_tool_actions(self):
        observed = []
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "conversation"}}]}).encode()
        def opener(request, timeout):
            observed.append((request, timeout))
            return Response()
        client = LMStudioChatClient("forge-30b", opener=opener, timeout_seconds=300, response_kind="conversation")
        self.assertEqual(client((("system", "rules"), ("user", "wunsch"))), "conversation")
        payload = json.loads(observed[0][0].data)
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], CONVERSATION_SCHEMA)
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(observed[0][1], 300)
        with self.assertRaises(ValueError):
            client.set_tool_actions(("finish",))
