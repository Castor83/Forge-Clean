"""Tool-free role schemas and bounded network response reads."""

import json
import unittest

from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.session_schema import SESSION_DRAFT_SCHEMA, SESSION_REVIEW_SCHEMA


class Response:
    def __init__(self, raw):
        self.raw = raw
        self.limits = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, maximum):
        self.limits.append(maximum)
        return self.raw[:maximum]


class SessionModelClientTests(unittest.TestCase):
    def test_separate_roles_send_closed_schemas_and_read_with_limit(self):
        for kind, schema in (("handoff_draft", SESSION_DRAFT_SCHEMA),
                             ("handoff_review", SESSION_REVIEW_SCHEMA)):
            response = Response(json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode())
            requests = []

            def opener(request, timeout):
                requests.append(json.loads(request.data))
                self.assertEqual(timeout, 300)
                return response

            with self.subTest(kind=kind):
                client = LMStudioChatClient("forge-30b", opener=opener,
                                            timeout_seconds=300, response_kind=kind)
                self.assertEqual(client((("system", "Host role"), ("user", "Sources"))), "{}")
                self.assertEqual(requests[0]["response_format"]["json_schema"]["schema"], schema)
                self.assertNotIn("tools", requests[0])
                self.assertEqual(response.limits, [100001])
                with self.assertRaises(ValueError):
                    client.set_tool_actions(("finish",))

    def test_oversized_handoff_reply_is_rejected_before_json_parse(self):
        response = Response(b"x" * 100001)
        client = LMStudioChatClient("forge-30b", response_kind="handoff_review",
                                    opener=lambda *args, **kwargs: response)
        with self.assertRaises(RuntimeError):
            client((("system", "Review"), ("user", "Sources")))
        self.assertEqual(response.limits, [100001])


if __name__ == "__main__":
    unittest.main()
