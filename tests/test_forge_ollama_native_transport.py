"""The Ollama adapter retains Forge schemas and sends an explicit 4K context."""

import io
import json
import unittest

from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.ollama_runtime import OllamaRuntime
from test_forge_ollama_runtime import MODEL, ProviderTransport, Response


NATIVE_ENDPOINT = "http://127.0.0.1:11434/api/chat"
MESSAGES = (("system", "Offline fixture."), ("user", "Keep the result unchanged."))
CONTENT = '{"action":"finish","passed":false,"summary":"ready","findings":[]}'


class NativeTransport(ProviderTransport):
    def __init__(self):
        super().__init__()
        self.native_response = {"model": MODEL, "done": True, "done_reason": "stop",
                                "message": {"role": "assistant", "content": CONTENT}}
        self.native_read_sizes = []

    def __call__(self, request, timeout):
        if request.full_url != NATIVE_ENDPOINT:
            return super().__call__(request, timeout)
        self.requests.append((request.full_url, request.get_method(), json.loads(request.data)))
        raw = self.native_response
        if not isinstance(raw, bytes):
            raw = json.dumps(raw).encode("utf-8")
        read_sizes = self.native_read_sizes

        class NativeResponse(io.BytesIO):
            def read(self, size=-1):
                read_sizes.append(size)
                return super().read(size)

        return NativeResponse(raw)


class OllamaNativeTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport = NativeTransport()
        self.runtime = OllamaRuntime(opener=self.transport)
        self.runtime.start()
        self.transport.requests.clear()

    def native_posts(self):
        return [payload for url, method, payload in self.transport.requests
                if url == NATIVE_ENDPOINT and method == "POST"]

    def assert_rejected_without_retry(self, data):
        self.transport.native_response = data
        client = self.runtime.create_chat_client()
        self.transport.requests.clear()
        with self.assertRaises(ValueError):
            client(MESSAGES)
        posts = [url for url, method, _ in self.transport.requests if method == "POST"]
        self.assertEqual(posts, [NATIVE_ENDPOINT])

    def test_all_five_forge_schemas_and_messages_reach_native_format_unchanged(self):
        for kind in ("tool_action", "subagent_result", "conversation", "handoff_draft", "handoff_review"):
            with self.subTest(response_kind=kind):
                reference = []

                def record_openai(request, timeout):
                    reference.append(json.loads(request.data))
                    return Response({"choices": [{"message": {"content": CONTENT}}]})

                LMStudioChatClient(MODEL, response_kind=kind, opener=record_openai)(MESSAGES)
                client = self.runtime.create_chat_client(response_kind=kind)
                self.transport.requests.clear()
                self.assertEqual(client(MESSAGES), CONTENT)
                posts = self.native_posts()
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0]["format"], reference[0]["response_format"]["json_schema"]["schema"])
                self.assertEqual(posts[0]["messages"], reference[0]["messages"])
                self.assertEqual(posts[0]["model"], MODEL)
                self.assertIs(posts[0]["stream"], False)
                self.assertEqual(posts[0]["keep_alive"], "10m")
                self.assertEqual(posts[0]["options"], {"num_ctx": 4096, "temperature": 0,
                                                     "seed": 42, "num_batch": 512})
                self.assertFalse(any("/v1/" in url for url, _, _ in self.transport.requests))

    def test_reused_client_sets_fixed_context_on_every_native_call(self):
        client = self.runtime.create_chat_client()
        self.transport.requests.clear()
        for _ in range(3):
            self.assertEqual(client(MESSAGES), CONTENT)
        posts = self.native_posts()
        self.assertEqual(len(posts), 3)
        self.assertEqual([payload["options"]["num_ctx"] for payload in posts], [4096, 4096, 4096])

    def test_adapter_preserves_negative_model_result_without_creating_success(self):
        self.transport.native_response["message"]["content"] = '{"passed":false,"summary":"not approved"}'
        text = self.runtime.create_chat_client()(MESSAGES)
        self.assertEqual(text, '{"passed":false,"summary":"not approved"}')

    def test_native_incomplete_response_is_rejected(self):
        self.transport.native_response["done"] = False
        self.assert_rejected_without_retry(self.transport.native_response)

    def test_native_truncated_response_is_rejected(self):
        self.transport.native_response["done_reason"] = "length"
        self.assert_rejected_without_retry(self.transport.native_response)

    def test_native_error_is_rejected(self):
        self.assert_rejected_without_retry({"error": "offline fixture failure"})

    def test_native_invalid_json_and_utf8_are_rejected(self):
        for raw in (b"not-json", b"\xff"):
            with self.subTest(raw=raw):
                self.assert_rejected_without_retry(raw)

    def test_native_invalid_message_is_rejected(self):
        for message in (None, [], {}, {"role": "user", "content": CONTENT},
                        {"role": "assistant", "content": ""}, {"role": "assistant", "content": 7}):
            with self.subTest(message=message):
                data = {"model": MODEL, "done": True, "done_reason": "stop", "message": message}
                self.assert_rejected_without_retry(data)

    def test_native_wrong_response_model_is_rejected(self):
        self.transport.native_response["model"] = "other:7b"
        self.assert_rejected_without_retry(self.transport.native_response)

    def test_native_response_read_is_bounded_to_one_megabyte(self):
        self.assert_rejected_without_retry(b" " * 1_000_001)
        self.assertEqual(self.transport.native_read_sizes, [1_000_001])

    def test_mutated_client_model_cannot_send_another_model_request(self):
        client = self.runtime.create_chat_client()
        client.model = "other:7b"
        self.transport.requests.clear()
        with self.assertRaises(ValueError):
            client(MESSAGES)
        self.assertFalse(any(method == "POST" for _, method, _ in self.transport.requests))

    def test_native_schema_retains_explicit_execution_copy_field_order(self):
        client = self.runtime.create_chat_client(execution_copy_field_order=True)
        client(MESSAGES)
        posts = self.native_posts()
        self.assertEqual(len(posts), 1)
        actions = posts[0]["format"]["anyOf"]
        write_schema = next(item for item in actions if item["properties"]["action"]["const"] == "write_file")
        self.assertEqual(list(write_schema["properties"]), ["action", "path", "content"])


if __name__ == "__main__":
    unittest.main()
