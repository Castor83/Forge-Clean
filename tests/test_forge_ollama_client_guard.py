"""A saved client must not autoload a changed or unloaded Ollama model."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime import cli as build_cli
from src.forge_runtime import conversation_cli, ollama_cli
from src.forge_runtime.contracts import PipelineResult, SubagentResult
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.ollama_runtime import OllamaRuntime
from test_forge_ollama_runtime import MODEL, ProviderTransport


CONFIGURED_ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
ENDPOINT = "http://127.0.0.1:11434/api/chat"
MESSAGES = (("user", "Offline transport check."),)


class ChangingTransport(ProviderTransport):
    def __init__(self):
        super().__init__()
        self.after_chat = None

    def __call__(self, request, timeout):
        response = super().__call__(request, timeout)
        if request.full_url == ENDPOINT and self.after_chat is not None:
            self.after_chat(self)
        return response


def unload(transport):
    transport.loaded = []


def change_context(transport):
    transport.loaded[0]["context_length"] = 8192


def change_loaded_digest(transport):
    transport.loaded[0]["digest"] = "b" * 64


def change_installed_digest(transport):
    transport.catalog[0]["digest"] = "b" * 64


def load_other_model(transport):
    transport.loaded = [{"name": "other:7b", "context_length": 4096}]


class OllamaClientGuardTests(unittest.TestCase):
    def setUp(self):
        self.transport = ChangingTransport()
        self.runtime = OllamaRuntime(opener=self.transport)
        self.runtime.start()
        self.client = self.runtime.create_chat_client()
        self.transport.requests.clear()

    def assert_change_blocks_chat(self, change):
        change(self.transport)
        with self.assertRaises(ValueError):
            self.client(MESSAGES)
        self.assertFalse(any(method == "POST" for _, method, _ in self.transport.requests))

    def test_unload_after_client_creation_blocks_chat_without_loading_again(self):
        self.assert_change_blocks_chat(unload)

    def test_context_change_after_client_creation_blocks_chat(self):
        self.assert_change_blocks_chat(change_context)

    def test_loaded_digest_change_after_client_creation_blocks_chat(self):
        self.assert_change_blocks_chat(change_loaded_digest)

    def test_installed_digest_change_after_client_creation_blocks_chat(self):
        self.assert_change_blocks_chat(change_installed_digest)

    def test_other_loaded_model_after_client_creation_blocks_chat(self):
        self.assert_change_blocks_chat(load_other_model)

    def test_every_call_rechecks_state_after_previous_success(self):
        self.assertEqual(json.loads(self.client(MESSAGES))["action"], "finish")
        self.transport.requests.clear()
        self.assert_change_blocks_chat(unload)

    def test_changed_state_during_response_is_rejected_without_retry(self):
        for change in (unload, change_context, change_loaded_digest,
                       change_installed_digest, load_other_model):
            with self.subTest(change=change.__name__):
                transport = ChangingTransport()
                runtime = OllamaRuntime(opener=transport)
                runtime.start()
                client = runtime.create_chat_client()
                transport.requests.clear()
                transport.after_chat = change
                with self.assertRaises(ValueError):
                    client(MESSAGES)
                posts = [url for url, method, _ in transport.requests if method == "POST"]
                self.assertEqual(posts, [ENDPOINT])

    def guarded_factory(self):
        factory = getattr(self.runtime, "chat_factory", None)
        self.assertTrue(callable(factory), "The trusted Ollama client factory is missing")
        return factory

    def test_factory_rejects_different_model_before_model_contact(self):
        factory = self.guarded_factory()
        with self.assertRaises(ValueError):
            factory("other:7b", endpoint=CONFIGURED_ENDPOINT)
        self.assertEqual(self.transport.requests, [])

    def test_factory_rejects_different_endpoint_before_model_contact(self):
        factory = self.guarded_factory()
        with self.assertRaises(ValueError):
            factory(MODEL, endpoint="http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(self.transport.requests, [])

    def test_factory_retains_real_client_tool_configuration_and_timeout(self):
        client = self.guarded_factory()(MODEL, endpoint=CONFIGURED_ENDPOINT, timeout_seconds=300)
        client.set_tool_actions(("finish",))
        self.assertEqual(json.loads(client(MESSAGES))["action"], "finish")
        payload = next(payload for url, method, payload in self.transport.requests if url == ENDPOINT)
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(client.response_kind, "tool_action")
        self.assertEqual([item["properties"]["action"]["const"]
                          for item in payload["format"]["anyOf"]], ["finish"])
        self.assertEqual(client.timeout_seconds, 300)


class OllamaLauncherGuardTests(unittest.TestCase):
    def unguarded_offline_client(self, transport):
        def construct(*args, **kwargs):
            return LMStudioChatClient(*args, **kwargs, opener=transport)
        return construct

    def test_launcher_chat_guards_all_four_actual_role_requests(self):
        transport = ChangingTransport()
        runtime = OllamaRuntime(opener=transport)

        def dispatch(task, planner, builder):
            clients = (planner, builder, task.session_drafter, task.session_reviewer)
            for client in clients:
                self.assertEqual(json.loads(client(MESSAGES))["action"], "finish")
            schemas = [payload["format"] for url, method, payload in transport.requests if url == ENDPOINT]
            self.assertEqual(len(schemas), 4)
            self.assertEqual(schemas[0]["anyOf"][0]["required"], ["reply", "questions", "task"])
            self.assertEqual({item["properties"]["action"]["const"] for item in schemas[1]["anyOf"]},
                             {"read_file", "write_file", "replace_text", "replace_text_batch", "run_check", "finish"})
            self.assertEqual(schemas[2]["required"], [
                "goal", "rules", "decisions", "completed", "open_tasks", "changed_files",
                "checks", "processes", "risks", "assumptions", "discarded_approaches", "next_step",
            ])
            self.assertEqual(schemas[3]["required"], ["checks", "findings", "limitations"])
            roles = [client.response_kind for client in clients]
            self.assertEqual(roles, ["conversation", "tool_action", "handoff_draft", "handoff_review"])
            ready_state = copy.deepcopy(transport.loaded)
            for role, client in zip(roles, clients):
                with self.subTest(role=role):
                    transport.loaded = []
                    transport.requests.clear()
                    with self.assertRaises(ValueError):
                        client(MESSAGES)
                    self.assertFalse(any(method == "POST" for _, method, _ in transport.requests))
                transport.loaded = copy.deepcopy(ready_state)
            return 0

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "workspaces").mkdir()
            source = str(root / "src/forge_runtime/ollama_cli.py")
            with patch.object(ollama_cli, "__file__", source), \
                    patch.object(conversation_cli, "__file__", source), \
                    patch.object(ollama_cli, "OllamaRuntime", return_value=runtime), \
                    patch.object(conversation_cli, "LMStudioChatClient",
                                 side_effect=self.unguarded_offline_client(transport)), \
                    patch.object(conversation_cli, "interact", side_effect=dispatch), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ollama_cli.main([
                    "--action", "chat", "--workspace", str(root / "workspaces" / "chat"),
                ]), 0)

    def test_launcher_build_uses_actual_guarded_client(self):
        transport = ChangingTransport()
        runtime = OllamaRuntime(opener=transport)

        def dispatch(workspace, spec, client, **limits):
            client.set_tool_actions(("finish",))
            self.assertEqual(json.loads(client(MESSAGES))["action"], "finish")
            transport.loaded = []
            transport.requests.clear()
            with self.assertRaises(ValueError):
                client(MESSAGES)
            self.assertFalse(any(method == "POST" for _, method, _ in transport.requests))
            return PipelineResult((SubagentResult("builder", False, "Offline fixture", ()),))

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / "workspaces" / "build"
            workspace.mkdir(parents=True)
            task_file = workspace / "task.json"
            task_file.write_text(json.dumps({
                "task_id": "guard-test", "objective": "Offline transport fixture",
                "allowed_files": ["agent.py"],
                "checks": [{"kind": "python_syntax", "path": "agent.py"}],
            }), encoding="utf-8")
            source = str(root / "src/forge_runtime/ollama_cli.py")
            with patch.object(ollama_cli, "__file__", source), \
                    patch.object(build_cli, "__file__", source), \
                    patch.object(ollama_cli, "OllamaRuntime", return_value=runtime), \
                    patch.object(build_cli, "LMStudioChatClient",
                                 side_effect=self.unguarded_offline_client(transport)), \
                    patch.object(build_cli, "run_forge_task", side_effect=dispatch), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ollama_cli.main([
                    "--action", "run", "--workspace", str(workspace), "--task-file", str(task_file),
                ]), 2)


if __name__ == "__main__":
    unittest.main()
