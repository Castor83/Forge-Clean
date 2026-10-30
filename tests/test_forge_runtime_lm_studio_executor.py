import json
import unittest

from src.forge_runtime.contracts import SubagentResult, SubagentTask
from src.forge_runtime.lm_studio_executor import LMStudioChatClient, LMStudioExecutor


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.payload


class RecordingOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        return FakeResponse(self.response)


class TestLMStudioExecutor(unittest.TestCase):
    def setUp(self):
        self.task = SubagentTask(
            task_id="task-1",
            objective="Build a harmless example",
            allowed_files=("src/example.py",),
        )

    def test_calls_local_endpoint_and_parses_result(self):
        api_response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"role":"builder","passed":true,'
                            '"summary":"Built","findings":["Created example"]}'
                        )
                    }
                }
            ]
        }
        opener = RecordingOpener(json.dumps(api_response).encode("utf-8"))
        executor = LMStudioExecutor(
            model="qwen/qwen3-coder-30b",
            timeout_seconds=30,
            opener=opener,
        )

        result = executor("builder", self.task, ())

        self.assertEqual(
            result,
            SubagentResult(
                role="builder",
                passed=True,
                summary="Built",
                findings=("Created example",),
            ),
        )
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(timeout, 30.0)
        self.assertEqual(request.headers["Content-type"], "application/json")

        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "qwen/qwen3-coder-30b")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(
            payload["response_format"]["json_schema"]["name"],
            "subagent_result",
        )
        self.assertIs(payload["response_format"]["json_schema"]["strict"], True)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["messages"][1]["role"], "user")
        self.assertEqual(
            payload["messages"][1]["content"],
            '{"allowed_files":["src/example.py"],"completed_roles":[],'
            '"objective":"Build a harmless example",'
            '"role":"builder","task_id":"task-1"}\n',
        )

    def test_rejects_external_or_malformed_endpoints(self):
        endpoints = (
            "https://127.0.0.1:1234/v1/chat/completions",
            "http://example.com/v1/chat/completions",
            "http://127.0.0.1:1234/v1/models",
            "http://127.0.0.1:1234/v1/chat/completions?key=value",
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    LMStudioExecutor("model", endpoint=endpoint)

    def test_accepts_supported_loopback_hosts(self):
        endpoints = (
            "http://localhost:1234/v1/chat/completions",
            "http://127.0.0.1:1234/v1/chat/completions",
            "http://[::1]:1234/v1/chat/completions",
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                executor = LMStudioExecutor("model", endpoint=endpoint)
                self.assertEqual(executor.endpoint, endpoint)

    def test_rejects_invalid_configuration_types(self):
        class StringSubclass(str):
            pass

        invalid_arguments = (
            {"model": ""},
            {"model": StringSubclass("model")},
            {"model": "model", "endpoint": 1},
            {"model": "model", "timeout_seconds": 0},
            {"model": "model", "timeout_seconds": True},
            {"model": "model", "opener": None},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    LMStudioExecutor(**arguments)

    def test_transport_failure_is_reported(self):
        def failing_opener(request, timeout):
            raise OSError("offline")

        executor = LMStudioExecutor("model", opener=failing_opener)
        with self.assertRaisesRegex(RuntimeError, "local LM Studio request failed"):
            executor("builder", self.task, ())

    def test_invalid_api_response_is_reported(self):
        responses = (
            b"not json",
            b"{}",
            b'{"choices":[]}',
            b'{"choices":[{"message":{}}]}',
        )
        for response in responses:
            with self.subTest(response=response):
                executor = LMStudioExecutor("model", opener=RecordingOpener(response))
                with self.assertRaisesRegex(
                    RuntimeError, "local LM Studio returned an invalid response"
                ):
                    executor("builder", self.task, ())

    def test_invalid_model_content_is_rejected(self):
        api_response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"role":"reviewer","passed":true,'
                            '"summary":"Wrong role","findings":[]}'
                        )
                    }
                }
            ]
        }
        opener = RecordingOpener(json.dumps(api_response).encode("utf-8"))
        executor = LMStudioExecutor("model", opener=opener)

        with self.assertRaisesRegex(ValueError, "response role must match"):
            executor("builder", self.task, ())


class TestLMStudioChatClient(unittest.TestCase):
    def test_rejects_invalid_response_kind(self):
        with self.assertRaises(ValueError):
            LMStudioChatClient("model", response_kind="invalid")

    def test_returns_content_and_preserves_message_order(self):
        api_response = {
            "choices": [{"message": {"content": '{"action":"finish"}'}}]
        }
        opener = RecordingOpener(json.dumps(api_response).encode("utf-8"))
        client = LMStudioChatClient("model", opener=opener)
        messages = (
            ("system", "Instructions"),
            ("user", "Task"),
            ("assistant", "Question"),
            ("user", "Answer"),
        )

        result = client(messages)

        self.assertEqual(result, '{"action":"finish"}')
        request, _ = opener.calls[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            payload["response_format"]["json_schema"]["name"], "tool_action"
        )
        action_schemas = payload["response_format"]["json_schema"]["schema"][
            "anyOf"
        ]
        self.assertEqual(
            {
                schema["properties"]["action"]["const"]
                for schema in action_schemas
            },
            {
                "read_file",
                "write_file",
                "replace_text",
                "replace_text_batch",
                "run_check",
                "finish",
            },
        )
        replace_schema = next(
            schema
            for schema in action_schemas
            if schema["properties"]["action"]["const"] == "replace_text"
        )
        self.assertEqual(
            replace_schema["required"],
            ["action", "path", "old", "new"],
        )
        self.assertIs(replace_schema["additionalProperties"], False)
        batch_schema = next(
            schema
            for schema in action_schemas
            if schema["properties"]["action"]["const"] == "replace_text_batch"
        )
        replacements_schema = batch_schema["properties"]["replacements"]
        self.assertEqual(replacements_schema["minItems"], 1)
        self.assertEqual(replacements_schema["maxItems"], 16)
        self.assertIs(
            replacements_schema["items"]["additionalProperties"],
            False,
        )
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": "Instructions"},
                {"role": "user", "content": "Task"},
                {"role": "assistant", "content": "Question"},
                {"role": "user", "content": "Answer"},
            ],
        )

    def test_tool_action_schema_can_be_narrowed_by_the_host(self):
        api_response = {
            "choices": [{"message": {"content": '{"action":"finish"}'}}]
        }
        opener = RecordingOpener(json.dumps(api_response).encode("utf-8"))
        client = LMStudioChatClient("model", opener=opener)
        client.set_tool_actions(("read_file", "replace_text", "finish"))

        client((("system", "Instructions"), ("user", "Task")))

        request, _ = opener.calls[0]
        payload = json.loads(request.data.decode("utf-8"))
        schemas = payload["response_format"]["json_schema"]["schema"]["anyOf"]
        self.assertEqual(
            {
                schema["properties"]["action"]["const"]
                for schema in schemas
            },
            {"read_file", "replace_text", "finish"},
        )

    def test_rejects_invalid_tool_action_configuration(self):
        client = LMStudioChatClient("model")
        for actions in (
            (),
            ["finish"],
            ("read_file",),
            ("finish", "finish"),
            ("unknown", "finish"),
        ):
            with self.subTest(actions=actions):
                with self.assertRaises(ValueError):
                    client.set_tool_actions(actions)

    def test_rejects_invalid_message_shapes_and_types(self):
        class TupleSubclass(tuple):
            pass

        client = LMStudioChatClient(
            "model",
            opener=RecordingOpener(
                b'{"choices":[{"message":{"content":"ok"}}]}'
            ),
        )
        invalid_messages = (
            [],
            (),
            TupleSubclass((("user", "Task"),)),
            (("user",),),
            (("tool", "Task"),),
            (("user", ""),),
            (("user", 1),),
        )
        for messages in invalid_messages:
            with self.subTest(messages=messages):
                with self.assertRaises(ValueError):
                    client(messages)

    def test_rejects_non_string_or_empty_response_content(self):
        api_responses = (
            {"choices": [{"message": {"content": None}}]},
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": 1}}]},
        )
        for api_response in api_responses:
            with self.subTest(api_response=api_response):
                opener = RecordingOpener(json.dumps(api_response).encode("utf-8"))
                client = LMStudioChatClient("model", opener=opener)
                with self.assertRaisesRegex(RuntimeError, "invalid message content"):
                    client((("user", "Task"),))


if __name__ == "__main__":
    unittest.main()
