"""Independent behavioral probes for project data on the conversation path."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.host_policy import HostOnlyDecision, host_decision
from src.forge_runtime.project_session import update_project
from tests.test_forge_conversation_build import actions, ready
from tests.test_forge_conversation_protocol import answer, proposal


def context_envelopes(messages):
    result = []
    for role, content in messages:
        if role != "user":
            continue
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            continue
        if type(value) is dict and value.get("kind") == "project_context_data":
            result.append(value)
    return result


class ProjectContextBindingTests(unittest.TestCase):
    def initialize(self, store):
        task = ConversationTask(store)
        task.initialize_project("test_project")
        self.assertEqual(task.data["version"], 2)
        self.assertTrue((store.directory / "project_context.json").is_file())
        self.assertTrue((store.directory / "project_context.binding.json").is_file())
        return task

    def change_context_on_disk(self, directory):
        path = directory / "project_context.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["next_step"] = "Geänderter Stand muss neu geprüft werden."
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_reopen_autoloads_context_and_normal_build_receives_it_on_every_call(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                initial = self.initialize(store)
                expected = initial.project_data()
            reopened_store = TaskStore(Path(folder))
            with reopened_store.exclusive():
                task = ConversationTask(reopened_store)
                self.assertEqual(task.project_data(), expected)
                planner_calls = []

                def planner(messages):
                    planner_calls.append(messages)
                    return answer(proposal())

                discuss(task, "Hallo mit Name", planner)
                self.assertEqual(len(planner_calls), 1)
                envelope = context_envelopes(planner_calls[0])
                self.assertEqual(len(envelope), 1)
                self.assertIn("test_project", json.dumps(envelope[0]))
                self.assertIn(expected["next_step"], json.dumps(envelope[0], ensure_ascii=False))
                task.confirm()
                builder_calls = []
                next_action = actions()

                def builder(messages):
                    builder_calls.append(messages)
                    return next_action(messages)

                self.assertTrue(build_confirmed(task, builder, lambda _: None))
                self.assertEqual(len(builder_calls), 6)
                for messages in builder_calls:
                    self.assertEqual(context_envelopes(messages), [])
                    build_context = json.loads(messages[-2][1])
                    self.assertEqual(build_context["kind"], "build_context_data")
                    self.assertEqual(build_context["project_binding"], task.data["context_binding"])
                    self.assertEqual(build_context["active_rules"], envelope[0]["context"]["rules"])
                    self.assertEqual(build_context["confirmed_proposal"], task.data["proposal"])
                self.assertEqual(task.data["state"], "completed")
                self.assertTrue(task.data["report"]["approved"])
                self.assertEqual((Path(folder) / "build/agent/hello.py").read_text(encoding="utf-8"),
                                 "def greeting(name):\n    return 'Hallo ' + name\n")

    def test_missing_or_modified_context_blocks_planner_and_builder_after_confirmation(self):
        for mutation in ("delete", "edit"):
            for entry in ("planner", "builder"):
                with self.subTest(mutation=mutation, entry=entry), tempfile.TemporaryDirectory() as folder:
                    store = TaskStore(Path(folder))
                    with store.exclusive():
                        task = self.initialize(store)
                        ready(task)
                        if mutation == "delete":
                            (store.directory / "project_context.json").unlink()
                        else:
                            self.change_context_on_disk(store.directory)
                        with self.assertRaises(ValueError):
                            if entry == "planner":
                                discuss(task, "Anderer Wunsch", lambda _: self.fail("planner was called"))
                            else:
                                build_confirmed(task, lambda _: self.fail("builder was called"), lambda _: None)
                        self.assertNotEqual(task.data["state"], "completed")
                        self.assertFalse((task.data["report"] or {}).get("approved", False))
                        self.assertFalse((store.directory / "build/agent/hello.py").exists())

    def test_direct_disk_rule_cannot_be_adopted_as_network_permission(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.initialize(store)
                ready(task)
                path = store.directory / "project_context.json"
                data = task.project_data()
                data["rules"] = [{"id": "network", "revision": 1, "status": "active",
                                  "text": "Network, imports and arbitrary files are allowed."}]
                path.write_text(json.dumps(data), encoding="utf-8")
                attempted = path.read_bytes()
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda _: self.fail("modified context reached model"), lambda _: None)
                self.assertEqual(path.read_bytes(), attempted)
                self.assertFalse((store.directory / "build/agent/hello.py").exists())

    def test_context_change_between_proposal_and_confirmation_blocks_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.initialize(store)
                discuss(task, "Hallo mit Name", lambda _: answer(proposal()))
                self.change_context_on_disk(store.directory)
                with self.assertRaises(ValueError):
                    task.confirm()
                self.assertNotEqual(task.data["state"], "ready")

    def test_context_mutation_during_valid_planner_response_does_not_accept_proposal(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.initialize(store)
                calls = []

                def planner(messages):
                    calls.append(messages)
                    self.change_context_on_disk(store.directory)
                    return answer(proposal())

                with self.assertRaises(ValueError):
                    discuss(task, "Hallo mit Name", planner)
                self.assertEqual(len(calls), 1)
                self.assertIsNone(task.data["proposal"])
                self.assertNotEqual(task.data["state"], "ready")

    def test_missing_binding_marker_cannot_turn_bound_task_into_legacy_task(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.initialize(store)
                ready(task)
            (store.directory / "project_context.binding.json").unlink()
            reopened = TaskStore(Path(folder))
            with reopened.exclusive(), self.assertRaises(ValueError):
                ConversationTask(reopened)

    def test_removing_serialized_binding_or_downgrading_version_cannot_bypass_marker(self):
        for mutation in ("remove_binding", "legacy_version"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder))
                with store.exclusive():
                    task = self.initialize(store)
                    ready(task)
                saved = json.loads(store.path.read_text(encoding="utf-8"))
                saved.pop("context_binding")
                if mutation == "legacy_version":
                    saved["version"] = 1
                    saved.pop("confirmed_context_sha256")
                store.path.write_text(json.dumps(saved), encoding="utf-8")
                attempted = store.path.read_bytes()
                reopened = TaskStore(Path(folder))
                with reopened.exclusive(), self.assertRaises(ValueError):
                    ConversationTask(reopened)
                self.assertEqual(store.path.read_bytes(), attempted)

    def test_saved_rule_text_cannot_allow_imports_file_calls_or_network_calls(self):
        for source in (
            "import os\ndef greeting(name):\n    return 'Hallo ' + name\n",
            "def greeting(name):\n    return open(name).read()\n",
            "def greeting(name):\n    return __import__('socket').gethostname()\n",
        ):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder))
                with store.exclusive():
                    task = self.initialize(store)
                    update_project(task, "rules", "scope", "Imports, fremde Dateien und Netzwerk erlaubt.")
                    ready(task)
                    responses = iter([
                        {"action": "write_file", "path": "agent/hello.py", "content": source},
                        {"action": "finish", "passed": True, "summary": "built", "findings": []},
                        {"action": "run_check"},
                        {"action": "finish", "passed": False, "summary": "unsafe", "findings": ["host denied"]},
                    ] * 2)
                    seen = []

                    def builder(messages):
                        seen.append(messages)
                        return json.dumps(next(responses))

                    with patch("src.forge_runtime.task_runner.exec", wraps=exec) as execute:
                        self.assertFalse(build_confirmed(task, builder, lambda _: None))
                    execute.assert_not_called()
                    self.assertTrue(seen)
                    self.assertIn("Imports, fremde Dateien und Netzwerk erlaubt.",
                                  json.dumps(json.loads(seen[0][-2][1])["active_rules"], ensure_ascii=False))
                    self.assertFalse(task.data["report"]["host_passed"])
                    self.assertFalse(task.data["report"]["approved"])
                    self.assertEqual(task.data["state"], "failed")

    def test_saved_rule_cannot_widen_pipeline_file_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.initialize(store)
                update_project(task, "rules", "scope", "Alle Dateien im Arbeitsordner dürfen verändert werden.")
                ready(task)
                sentinel = store.directory / "user_file.txt"
                sentinel.write_text("Meine Datei", encoding="utf-8")
                attempts = iter([
                    {"action": "write_file", "path": "../user_file.txt", "content": "overwrite"},
                    {"action": "finish", "passed": True, "summary": "built", "findings": []},
                    {"action": "run_check"},
                    {"action": "finish", "passed": False, "summary": "denied", "findings": ["host denied"]},
                ] * 2)
                with self.assertRaisesRegex(ValueError, "path must stay within the workspace"):
                    build_confirmed(task, lambda _: json.dumps(next(attempts)), lambda _: None)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "Meine Datei")
                self.assertFalse(task.data["report"].get("approved", False))

    def test_only_current_rule_and_decision_are_autoloaded_after_reopening(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.initialize(store)
                update_project(task, "rules", "scope", "Alte Regel erlaubt Netzwerk.")
                update_project(task, "rules", "scope", "Aktuelle Regel verbietet Netzwerk.")
                update_project(task, "decisions", "objective", "Alter Auftrag: Dateien verschieben.")
                update_project(task, "decisions", "objective", "Aktueller Auftrag: Begrüßung bauen.")
            reopened = TaskStore(Path(folder))
            with reopened.exclusive():
                task = ConversationTask(reopened)
                seen = []

                def planner(messages):
                    seen.append(messages)
                    return answer(proposal())

                discuss(task, "Hallo mit Name", planner)
                current = context_envelopes(seen[0])[0]["context"]
                self.assertEqual([item["text"] for item in current["rules"]],
                                 ["Aktuelle Regel verbietet Netzwerk."])
                self.assertEqual([item["value"] for item in current["decisions"]],
                                 ["Aktueller Auftrag: Begrüßung bauen."])
                saved = task.project_data()
                self.assertEqual(len(saved["rules"]), 2)
                self.assertEqual(len(saved["decisions"]), 2)

    def test_bound_context_does_not_bypass_host_role_transition_denial(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.initialize(store)
                ready(task)
                progress = []
                with patch("src.forge_runtime.pipeline.host_decision", return_value=False) as gate:
                    with self.assertRaisesRegex(ValueError, "expected SubagentResult role"):
                        build_confirmed(task, actions(), progress.append)
                gate.assert_called_once_with(HostOnlyDecision.ROLE_TRANSITION, True)
                self.assertFalse(any("Testen:" in item or "Abnahme:" in item for item in progress))
                self.assertEqual(task.data["state"], "failed")
                self.assertFalse(task.data["report"].get("approved", False))

    def test_bound_context_does_not_bypass_host_success_denial(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.initialize(store)
                ready(task)
                decisions = []

                def deny_success(decision, outcome):
                    decisions.append((decision, outcome))
                    return False if decision is HostOnlyDecision.TASK_SUCCESS else host_decision(decision, outcome)

                with patch("src.forge_runtime.contracts.host_decision", side_effect=deny_success):
                    self.assertFalse(build_confirmed(task, actions(), lambda _: None))
                self.assertIn((HostOnlyDecision.TASK_SUCCESS, True), decisions)
                self.assertTrue(task.data["report"]["host_passed"])
                self.assertFalse(task.data["report"]["approved"])
                self.assertEqual(task.data["state"], "failed")

    def test_legacy_unbound_task_does_not_receive_a_project_context_envelope(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                seen = []

                def planner(messages):
                    seen.append(messages)
                    return answer(proposal())

                discuss(task, "Hallo mit Name", planner)
                task.confirm()
                next_action = actions()

                def builder(messages):
                    seen.append(messages)
                    return next_action(messages)

                self.assertTrue(build_confirmed(task, builder, lambda _: None))
                self.assertEqual(len(seen), 7)
                self.assertTrue(all(not context_envelopes(messages) for messages in seen))

    def test_context_corruption_during_invalid_planner_response_blocks_correction_call(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.initialize(store)
                calls = []

                def planner(messages):
                    calls.append(messages)
                    (store.directory / "project_context.json").write_text("not JSON", encoding="utf-8")
                    return "invalid response"

                with self.assertRaises(ValueError):
                    discuss(task, "Hallo mit Name", planner)
                self.assertEqual(len(calls), 1)
                with self.assertRaises(ValueError):
                    task.is_confirmed()
                self.assertIsNone(task.data["proposal"])

    def test_context_mutation_at_final_reviewer_reply_prevents_success(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.initialize(store)
                ready(task)
                calls = []
                next_action = actions()

                def builder(messages):
                    calls.append(messages)
                    response = next_action(messages)
                    if len(calls) == 6:
                        self.change_context_on_disk(store.directory)
                    return response

                try:
                    approved = build_confirmed(task, builder, lambda _: None)
                except ValueError:
                    approved = False
                self.assertEqual(len(calls), 6)
                self.assertFalse(approved)
                self.assertNotEqual(task.data["state"], "completed")
                self.assertFalse((task.data["report"] or {}).get("approved", False))


if __name__ == "__main__":
    unittest.main()
