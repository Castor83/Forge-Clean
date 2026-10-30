"""Provider selection is offline; only the injected transport sees requests."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.model_manager import (
    ModelAdvisor, ModelManager, ModelSelection, ProviderEvidence, ProviderProfile,
)
from src.forge_runtime.supervisor import ForgeTaskExecutor
from src.forge_runtime.task_contract import Task


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return b'{"choices":[{"message":{"content":"offline result"}}]}'


class RecordingTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return Response()


def lm_profile(**changes):
    values = dict(
        profile_id="lmstudio-forge30b", provider_id="lmstudio", model_id="forge-30b",
        endpoint="http://127.0.0.1:1234/v1/chat/completions",
        context_length=32768, parallel=1, temperature=0,
        model_identity="Qwen3-Coder-30B Q4_K_M; source not freshly verified",
    )
    values.update(changes)
    return ProviderProfile(**values)


class TestProviderProfiles(unittest.TestCase):
    def test_rejects_remote_credentials_malformed_ports_and_unregistered_provider(self):
        invalid = (
            {"provider_id": "cloud"}, {"profile_id": "../profile"},
            {"model_id": " model "}, {"model_id": "a\nmodel"},
            {"endpoint": "https://127.0.0.1:1234/v1/chat/completions"},
            {"endpoint": "http://example.com:1234/v1/chat/completions"},
            {"endpoint": "http://user:secret@127.0.0.1:1234/v1/chat/completions"},
            {"endpoint": "http://127.0.0.1:99999/v1/chat/completions"},
            {"endpoint": "http://127.0.0.1:abc/v1/chat/completions"},
            {"endpoint": "http://127.0.0.1:0/v1/chat/completions"},
            {"endpoint": "http://127.0.0.1/v1/chat/completions"},
            {"endpoint": "http://127.0.0.1:1234/v1/chat/completions?key=secret"},
            {"endpoint": "http://127.0.0.1:1234/v1/chat/completions#fragment"},
            {"endpoint": "http://127.0.0.1:1234/v1/models"},
            {"endpoint": " http://127.0.0.1:1234/v1/chat/completions"},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                lm_profile(**change)

    def test_keeps_fixed_transport_and_serial_settings(self):
        for change in (
            {"context_length": 0}, {"context_length": True},
            {"parallel": 2}, {"parallel": True},
            {"temperature": 0.7}, {"temperature": True},
            {"temperature": float("nan")}, {"model_identity": ""},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                lm_profile(**change)

    def test_profile_is_immutable_and_inventory_is_not_live_verification(self):
        profile = lm_profile()
        self.assertEqual(profile.verification_status, "not_tested")
        with self.assertRaises(FrozenInstanceError):
            profile.model_id = "other"

    def test_evidence_requires_bound_backend_model_contract_time_and_path(self):
        values = dict(
            status="offline_contract_passed", backend_version="fixture-v1",
            model_identity="fixture-model-sha256:" + "a" * 64,
            test_contract="offline provider transport v1",
            observed_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
            evidence_path="workspaces/provider-offline-green-20261013.log",
            profile_fingerprint=lm_profile(
                model_identity="fixture-model-sha256:" + "a" * 64).fingerprint,
            model_digest="a" * 64, model_digest_kind="offline_fixture_sha256",
        )
        evidence = ProviderEvidence(**values)
        self.assertEqual(lm_profile(model_identity=evidence.model_identity,
                                    evidence=evidence).verification_status,
                         "offline_contract_passed")
        for change in (
            {"status": "completed"}, {"backend_version": ""},
            {"model_identity": ""}, {"test_contract": ""},
            {"observed_at": datetime(2026, 10, 13)},
            {"evidence_path": "../outside.log"},
            {"evidence_path": "C:/outside.log"},
            {"profile_fingerprint": "bad"}, {"model_digest": "bad"},
            {"model_digest_kind": "gguf"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ProviderEvidence(**(values | change))

    def test_evidence_cannot_be_attached_to_a_different_model_identity(self):
        evidence = ProviderEvidence(
            "loaded", "fixture-v1", "model-A", "contract-v1",
            datetime(2026, 10, 13, tzinfo=timezone.utc), "workspaces/evidence.json",
            lm_profile(model_identity="model-A").fingerprint,
            "a" * 64, "gguf_sha256",
        )
        with self.assertRaises(ValueError):
            lm_profile(model_identity="model-B", evidence=evidence)

    def test_evidence_cannot_transfer_to_another_provider_profile_model_or_settings(self):
        evidence = ProviderEvidence(
            "functional_build_passed", "fixture-v1", lm_profile().model_identity,
            "contract-v1", datetime(2026, 10, 13, tzinfo=timezone.utc),
            "workspaces/evidence.json",
            lm_profile().fingerprint, "a" * 64, "gguf_sha256",
        )
        for change in ({"provider_id": "ollama"}, {"profile_id": "other-profile"},
                       {"model_id": "other-model"}, {"context_length": 8192},
                       {"endpoint": "http://127.0.0.1:11434/v1/chat/completions"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                lm_profile(evidence=evidence, **change)

    def test_offline_fixture_cannot_be_labelled_as_a_live_model_success(self):
        for status in ("loaded", "schema_smoke_passed", "functional_build_passed"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                ProviderEvidence(
                    status, "fixture-v1", lm_profile().model_identity, "contract-v1",
                    datetime(2026, 10, 13, tzinfo=timezone.utc), "workspaces/evidence.json",
                    lm_profile().fingerprint, "a" * 64, "offline_fixture_sha256",
                )


class TestOfflineSelection(unittest.TestCase):
    def setUp(self):
        self.lm = lm_profile()
        self.ollama = lm_profile(
            profile_id="ollama-local14b", provider_id="ollama",
            model_id="huihui_ai/qwen2.5-coder-abliterate:14b",
            endpoint="http://127.0.0.1:11434/v1/chat/completions",
            model_identity="ollama-manifest:ac9889de; no live build test",
        )
        self.manager = ModelManager((self.lm, self.ollama))

    def test_registry_lists_only_known_models_and_never_chooses_a_default(self):
        self.assertEqual(self.manager.list_profiles(), (self.lm, self.ollama))
        self.assertEqual(self.manager.list_models("lmstudio"), ("forge-30b",))
        self.assertEqual(self.manager.list_models("ollama"),
                         ("huihui_ai/qwen2.5-coder-abliterate:14b",))
        with self.assertRaises(ValueError):
            self.manager.list_models("unknown")
        with self.assertRaises(ValueError):
            self.manager.select_model(None)

    def test_unknown_or_cross_provider_choice_fails_without_fallback(self):
        for choice in (ModelSelection("ollama", "forge-30b"),
                       ModelSelection("lmstudio", "unknown")):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                self.manager.select_model(choice)
        self.assertEqual(self.manager.select_model(ModelSelection("lmstudio", "forge-30b")),
                         self.lm)

    def test_selection_rejects_unregistered_provider_and_empty_or_dirty_model(self):
        for values in (("cloud", "m"), ("lmstudio", ""), ("ollama", " m "),
                       ("lmstudio", True), ("ollama", "a\nmodel")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ModelSelection(*values)

    def test_duplicate_ids_or_ambiguous_provider_model_fail_at_registration(self):
        for profiles in (
            (self.lm, replace(self.ollama, profile_id=self.lm.profile_id)),
            (self.lm, replace(self.lm, profile_id="second-profile")),
            [self.lm], (), (object(),),
        ):
            with self.subTest(profiles=profiles), self.assertRaises(ValueError):
                ModelManager(profiles)

    def test_advisor_does_not_activate_a_choice_or_upgrade_evidence(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             patch("subprocess.Popen", side_effect=AssertionError("process")):
            advice = ModelAdvisor(self.manager).recommend("ollama")
            self.assertEqual(len(advice), 1)
            self.assertEqual(advice[0].selection,
                             ModelSelection("ollama", self.ollama.model_id))
            self.assertEqual(advice[0].verification_status, "not_tested")
            self.assertTrue(advice[0].reason)
            self.assertEqual(self.manager.select_model(advice[0].selection), self.ollama)

    def test_factory_creation_and_selection_do_not_contact_or_load_models(self):
        def forbidden_transport(*args, **kwargs):
            self.fail("Factory or selection contacted the provider")
        with patch("subprocess.Popen", side_effect=AssertionError("process")):
            choice = ModelSelection("ollama", self.ollama.model_id)
            self.manager.select_model(choice)
            client = self.manager.create_chat_client(choice, opener=forbidden_transport)
            self.assertEqual(client.model, self.ollama.model_id)
            self.assertEqual(client.endpoint,
                             "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(self.ollama.verification_status, "not_tested")

    def test_bound_supervisor_factory_rejects_model_change_before_transport(self):
        transport = RecordingTransport()
        factory = self.manager.bind_chat_factory(
            ModelSelection("ollama", self.ollama.model_id), opener=transport,
        )
        with self.assertRaises(ValueError):
            factory("forge-30b")
        self.assertEqual(transport.requests, [])
        client = factory(self.ollama.model_id)
        self.assertEqual(client((("user", "test"),)), "offline result")
        payload = json.loads(transport.requests[0][0].data)
        self.assertEqual(payload["model"], self.ollama.model_id)

    def test_invalid_client_options_fail_even_before_factory_is_bound(self):
        choice = ModelSelection("lmstudio", "forge-30b")
        for options in ({"timeout_seconds": float("nan")},
                        {"timeout_seconds": float("inf")},
                        {"timeout_seconds": True}, {"response_kind": "invalid"},
                        {"opener": None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.manager.bind_chat_factory(choice, **options)


class TestProviderRequestCompatibility(unittest.TestCase):
    def test_selected_provider_factory_completes_real_forge_host_build_offline(self):
        # Removing model binding, corrupting the emitted code, or bypassing the
        # real host tests must break this integration, not merely a fake response.
        source = Path(__file__).parents[1]
        spec = json.loads((source / "examples/hello_agent.json").read_text())
        requests = []
        started = set()

        class BuildResponse(Response):
            def __init__(self, content):
                self.content = content

            def read(self, size=-1):
                return json.dumps({"choices": [{"message": {"content": self.content}}]}).encode()

        def transport(request, timeout):
            requests.append(request)
            payload = json.loads(request.data)
            self.assertEqual(payload["model"], "forge-30b")
            self.assertEqual(request.full_url, "http://127.0.0.1:1234/v1/chat/completions")
            role = json.loads(payload["messages"][1]["content"])["role"]
            if role not in started:
                started.add(role)
                action = ({"action": "write_file", "path": "agent/hello.py",
                           "content": "def greeting(name):\n    return 'Hello, ' + name\n"}
                          if role == "builder" else {"action": "run_check"})
            else:
                action = {"action": "finish", "passed": True,
                          "summary": "Offline role complete", "findings": []}
            return BuildResponse(json.dumps(action))

        with tempfile.TemporaryDirectory(dir=source / "workspaces") as folder:
            root = Path(folder)
            (root / "build").mkdir()
            factory = ModelManager((lm_profile(),)).bind_chat_factory(
                ModelSelection("lmstudio", "forge-30b"), opener=transport,
                timeout_seconds=300,
            )
            task = Task.create(spec["task_id"], spec["objective"],
                               {"workspace": "build", "task_spec": spec}, "forge-30b")
            outcome = ForgeTaskExecutor(root, factory)(task)
            self.assertTrue(outcome.approved)
            self.assertIsNone(outcome.error)
            self.assertEqual((root / "build/agent/hello.py").read_text(),
                             "def greeting(name):\n    return 'Hello, ' + name\n")
            self.assertEqual(started, {"builder", "tester", "reviewer"})
            self.assertEqual(len(requests), 6)

    def test_lmstudio_bytes_headers_and_timeouts_match_all_existing_response_kinds(self):
        manager = ModelManager((lm_profile(),))
        messages = (("system", "Host instructions"), ("user", "Grüße"),
                    ("assistant", "Prior content"), ("user", "Next"))
        for kind in ("tool_action", "subagent_result", "conversation",
                     "handoff_draft", "handoff_review"):
            with self.subTest(kind=kind):
                direct_transport, managed_transport = RecordingTransport(), RecordingTransport()
                direct = LMStudioChatClient("forge-30b", timeout_seconds=300,
                                            response_kind=kind, opener=direct_transport)
                managed = manager.create_chat_client(
                    ModelSelection("lmstudio", "forge-30b"), timeout_seconds=300,
                    response_kind=kind, opener=managed_transport,
                )
                self.assertEqual(direct(messages), managed(messages))
                old, old_timeout = direct_transport.requests[0]
                new, new_timeout = managed_transport.requests[0]
                self.assertEqual(new.data, old.data)
                self.assertEqual(new.full_url, "http://127.0.0.1:1234/v1/chat/completions")
                self.assertEqual(new.headers, old.headers)
                self.assertEqual((new.method, new_timeout, old_timeout), ("POST", 300, 300))

    def test_narrowed_actions_and_execution_copy_order_remain_byte_identical(self):
        manager = ModelManager((lm_profile(),))
        direct_transport, managed_transport = RecordingTransport(), RecordingTransport()
        direct = LMStudioChatClient("forge-30b", opener=direct_transport,
                                    execution_copy_field_order=True)
        managed = manager.create_chat_client(ModelSelection("lmstudio", "forge-30b"),
                                             opener=managed_transport,
                                             execution_copy_field_order=True)
        for client in (direct, managed):
            client.set_tool_actions(("read_file", "replace_text", "finish"))
            client((("user", "test"),))
        self.assertEqual(managed_transport.requests[0][0].data,
                         direct_transport.requests[0][0].data)
        payload = json.loads(managed_transport.requests[0][0].data)
        schemas = payload["response_format"]["json_schema"]["schema"]["anyOf"]
        self.assertEqual([schema["properties"]["action"]["const"] for schema in schemas],
                         ["read_file", "replace_text", "finish"])
        self.assertEqual(list(schemas[1]["properties"]), ["action", "path", "old", "new"])

    def test_ollama_uses_selected_model_and_nested_schema_without_extra_request_options(self):
        profile = lm_profile(provider_id="ollama", profile_id="ollama-fixture",
                             model_id="fixture:14b",
                             endpoint="http://127.0.0.1:11434/v1/chat/completions")
        transport = RecordingTransport()
        client = ModelManager((profile,)).create_chat_client(
            ModelSelection("ollama", "fixture:14b"), opener=transport,
        )
        client((("user", "test"),))
        request = transport.requests[0][0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(payload["model"], "fixture:14b")
        self.assertEqual(set(payload),
                         {"model", "messages", "response_format", "stream", "temperature"})
        self.assertIs(payload["response_format"]["json_schema"]["strict"], True)
        self.assertIn("anyOf", payload["response_format"]["json_schema"]["schema"])
        self.assertEqual(payload["temperature"], 0)
        self.assertIs(payload["stream"], False)


if __name__ == "__main__":
    unittest.main()
