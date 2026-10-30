"""Protect the installed-only, explicit Ollama model lifecycle and transport."""

import json
import unittest

try:
    from src.forge_runtime.ollama_runtime import OllamaRuntime
except ImportError:
    OllamaRuntime = None


MODEL = "huihui_ai/qwen2.5-coder-abliterate:14b"
DIGEST = "ac9889decebb11a8bd5bddcc52553843f521fd87f1d8d210e4b6cfe5da77f522"


class Response:
    def __init__(self, data):
        self.data = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return self.data


class ProviderTransport:
    """Replace only the external HTTP boundary, including its state changes."""

    def __init__(self):
        self.catalog = [{"name": MODEL, "model": MODEL, "digest": DIGEST,
                         "size": 8988124423, "details": {"format": "gguf",
                         "parameter_size": "14.8B", "quantization_level": "Q4_K_M"}}]
        self.loaded = []
        self.requests = []

    def __call__(self, request, timeout):
        payload = None if request.data is None else json.loads(request.data)
        self.requests.append((request.full_url, request.get_method(), payload))
        if request.full_url == "http://127.0.0.1:11434/api/version":
            return Response({"version": "0.40.3"})
        if request.full_url == "http://127.0.0.1:11434/api/tags":
            return Response({"models": self.catalog})
        if request.full_url == "http://127.0.0.1:11434/api/ps":
            return Response({"models": self.loaded})
        if request.full_url == "http://127.0.0.1:11434/api/generate":
            if payload.get("keep_alive") == 0:
                self.loaded = [item for item in self.loaded if item["name"] != MODEL]
                return Response({"done": True, "done_reason": "unload"})
            self.loaded = [{"name": MODEL, "model": MODEL, "digest": DIGEST,
                            "context_length": payload["options"]["num_ctx"],
                            "size": 9470098799, "size_vram": 9470098799,
                            "runner": "llamacpp"}]
            return Response({"done": True, "done_reason": "load"})
        if request.full_url == "http://127.0.0.1:11434/v1/chat/completions":
            return Response({"choices": [{"message": {
                "content": '{"action":"finish","passed":false,"summary":"ready","findings":[]}'},
                "finish_reason": "stop"}]})
        if request.full_url == "http://127.0.0.1:11434/api/chat":
            return Response({"model": MODEL, "done": True, "done_reason": "stop", "message": {
                "role": "assistant",
                "content": '{"action":"finish","passed":false,"summary":"ready","findings":[]}'}})
        raise AssertionError("Unexpected endpoint: " + request.full_url)


class OllamaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(OllamaRuntime, "The Ollama runtime connection is missing")
        self.transport = ProviderTransport()
        self.runtime = OllamaRuntime(opener=self.transport)

    def test_start_loads_only_installed_selected_model_and_checks_actual_context(self):
        result = self.runtime.start()
        self.assertEqual(result["loaded_model"]["name"], MODEL)
        self.assertEqual(result["loaded_model"]["context_length"], 4096)
        posts = [item for item in self.transport.requests if item[1] == "POST"]
        self.assertEqual(len(posts), 1)
        url, _, payload = posts[0]
        self.assertEqual(url, "http://127.0.0.1:11434/api/generate")
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["prompt"], "")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"]["num_ctx"], 4096)
        self.assertEqual(payload["options"]["temperature"], 0)

    def test_missing_installed_model_fails_without_download_or_load(self):
        self.transport.catalog = []
        with self.assertRaises(ValueError):
            self.runtime.start()
        self.assertFalse(any(method == "POST" for _, method, _ in self.transport.requests))

    def test_changed_model_digest_fails_before_model_contact(self):
        self.transport.catalog[0]["digest"] = "b" * 64
        with self.assertRaises(ValueError):
            self.runtime.start()
        self.assertFalse(any(method == "POST" for _, method, _ in self.transport.requests))

    def test_other_loaded_model_is_not_replaced(self):
        self.transport.loaded = [{"name": "other:7b", "context_length": 4096}]
        with self.assertRaises(ValueError):
            self.runtime.start()
        self.assertEqual(self.transport.loaded[0]["name"], "other:7b")
        self.assertFalse(any(method == "POST" for _, method, _ in self.transport.requests))

    def test_wrong_context_is_not_silently_reloaded(self):
        self.transport.loaded = [{"name": MODEL, "digest": DIGEST, "context_length": 8192}]
        with self.assertRaises(ValueError):
            self.runtime.start()
        self.assertEqual(self.transport.loaded[0]["context_length"], 8192)
        self.assertFalse(any(method == "POST" for _, method, _ in self.transport.requests))

    def test_already_ready_model_is_not_loaded_again(self):
        self.runtime.start()
        self.transport.requests.clear()
        self.runtime.start()
        self.assertFalse(any(method == "POST" for _, method, _ in self.transport.requests))

    def test_selected_forge_client_uses_ollama_endpoint_and_existing_strict_schema(self):
        self.runtime.start()
        client = self.runtime.create_chat_client(response_kind="tool_action")
        text = client((("user", "Return a technical connection result."),))
        self.assertEqual(json.loads(text)["action"], "finish")
        chat_requests = [item for item in self.transport.requests
                         if item[0] == "http://127.0.0.1:11434/api/chat"]
        self.assertEqual(len(chat_requests), 1)
        url, method, payload = chat_requests[0]
        self.assertEqual((url, method), ("http://127.0.0.1:11434/api/chat", "POST"))
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual({item["properties"]["action"]["const"] for item in payload["format"]["anyOf"]},
                         {"read_file", "write_file", "replace_text", "replace_text_batch", "run_check", "finish"})
        self.assertTrue(all(item["additionalProperties"] is False for item in payload["format"]["anyOf"]))
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertEqual(payload["options"]["num_ctx"], 4096)

    def test_unloaded_model_cannot_trigger_implicit_openai_autoload(self):
        with self.assertRaises(ValueError):
            self.runtime.create_chat_client()
        self.assertFalse(any(method == "POST" for _, method, _ in self.transport.requests))

    def test_stop_unloads_only_selected_model_and_never_stops_server(self):
        self.runtime.start()
        self.transport.loaded.append({"name": "other:7b", "context_length": 4096})
        self.runtime.stop()
        self.assertEqual([item["name"] for item in self.transport.loaded], ["other:7b"])
        self.assertEqual(self.transport.requests[-1][2]["keep_alive"], 0)
        self.assertEqual(self.transport.requests[-1][2]["model"], MODEL)

    def test_status_never_loads_or_changes_models(self):
        result = self.runtime.status()
        self.assertEqual(result["backend_version"], "0.40.3")
        self.assertFalse(any(method == "POST" for _, method, _ in self.transport.requests))


if __name__ == "__main__":
    unittest.main()
