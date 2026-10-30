import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime import conversation_cli as cli
from src.forge_runtime.lm_studio_executor import LMStudioChatClient


class ConversationProviderEndpointTests(unittest.TestCase):
    def run_conversation_start(self, endpoint=None):
        observed = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                raw = json.dumps({"choices": [{"message": {
                    "content": '{"offline_fixture":true}',
                }}]}).encode("utf-8")
                return raw if size < 0 else raw[:size]

        def opener(request, timeout):
            observed.append((request, timeout))
            return Response()

        def real_client(*args, **kwargs):
            return LMStudioChatClient(*args, **kwargs, opener=opener)

        def dispatch(task, planner, builder):
            clients = (planner, builder, task.session_drafter, task.session_reviewer)
            for client in clients:
                self.assertEqual(
                    client((("system", "Return one JSON object."), ("user", "Transport check."))),
                    '{"offline_fixture":true}',
                )
            return 0

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "workspaces").mkdir()
            argv = ["--workspace", str(root / "workspaces" / "endpoint-test"),
                    "--model", "huihui_ai/qwen2.5-coder-abliterate:14b"]
            if endpoint is not None:
                argv.extend(("--endpoint", endpoint))
            output = io.StringIO()
            with patch.object(cli, "__file__", str(root / "src/forge_runtime/conversation_cli.py")), \
                    patch.object(cli, "LMStudioChatClient", side_effect=real_client), \
                    patch.object(cli, "interact", side_effect=dispatch), \
                    redirect_stdout(output), redirect_stderr(output):
                try:
                    exit_code = cli.main(argv)
                except SystemExit as exc:
                    exit_code = exc.code
        return exit_code, observed, output.getvalue()

    def assert_role_requests(self, observed, endpoint):
        self.assertEqual(len(observed), 4)
        payloads = []
        for request, timeout in observed:
            self.assertEqual(request.full_url, endpoint)
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(timeout, 300)
            payload = json.loads(request.data)
            payloads.append(payload)
            self.assertEqual(payload["model"], "huihui_ai/qwen2.5-coder-abliterate:14b")
            self.assertEqual(payload["messages"], [
                {"role": "system", "content": "Return one JSON object."},
                {"role": "user", "content": "Transport check."},
            ])
            self.assertEqual(payload["response_format"]["type"], "json_schema")
            self.assertIs(payload["response_format"]["json_schema"]["strict"], True)
            self.assertIs(payload["stream"], False)
            self.assertEqual(payload["temperature"], 0)
        formats = [payload["response_format"]["json_schema"] for payload in payloads]
        self.assertEqual([item["name"] for item in formats], [
            "conversation", "tool_action", "handoff_draft", "handoff_review",
        ])
        self.assertEqual(formats[0]["schema"]["anyOf"][0]["required"], ["reply", "questions", "task"])
        self.assertEqual({item["properties"]["action"]["const"]
                          for item in formats[1]["schema"]["anyOf"]}, {
            "read_file", "write_file", "replace_text", "replace_text_batch", "run_check", "finish",
        })
        self.assertEqual(formats[2]["schema"]["required"], [
            "goal", "rules", "decisions", "completed", "open_tasks", "changed_files",
            "checks", "processes", "risks", "assumptions", "discarded_approaches", "next_step",
        ])
        self.assertEqual(formats[3]["schema"]["required"], ["checks", "findings", "limitations"])

    def test_explicit_endpoint_dispatches_all_four_roles_to_ollama(self):
        endpoint = "http://127.0.0.1:11434/v1/chat/completions"
        exit_code, observed, output = self.run_conversation_start(endpoint)
        self.assertEqual(exit_code, 0, output)
        self.assert_role_requests(observed, endpoint)

    def test_default_endpoint_dispatches_all_four_roles_to_lm_studio(self):
        exit_code, observed, output = self.run_conversation_start()
        self.assertEqual(exit_code, 0, output)
        self.assert_role_requests(observed, "http://127.0.0.1:1234/v1/chat/completions")

    def test_nonlocal_endpoint_is_rejected_before_any_network_request(self):
        exit_code, observed, output = self.run_conversation_start(
            "http://model-server.example:11434/v1/chat/completions"
        )
        self.assertEqual(exit_code, 1, output)
        self.assertIn("endpoint must be a local", output)
        self.assertEqual(observed, [])


if __name__ == "__main__":
    unittest.main()
