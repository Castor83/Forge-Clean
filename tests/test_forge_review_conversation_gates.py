import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.host_policy import HostOnlyDecision, host_decision
from tests.test_forge_conversation_build import actions, ready


class ReviewConversationGateTests(unittest.TestCase):
    def test_conversation_cannot_continue_after_host_role_transition_denial(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                progress = []
                with patch("src.forge_runtime.pipeline.host_decision", return_value=False) as gate:
                    with self.assertRaisesRegex(ValueError, "expected SubagentResult role"):
                        build_confirmed(task, actions(), progress.append)
                gate.assert_called_once_with(HostOnlyDecision.ROLE_TRANSITION, True)
                self.assertFalse(any("Testen:" in line or "Abnahme:" in line for line in progress))
                self.assertEqual(task.data["state"], "failed")
                self.assertFalse(task.data["report"].get("approved", False))

    def test_conversation_cannot_override_host_success_denial_with_passed_cases(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                decisions = []

                def deny_success(decision, outcome):
                    decisions.append((decision, outcome))
                    return False if decision is HostOnlyDecision.TASK_SUCCESS else host_decision(decision, outcome)

                with patch("src.forge_runtime.contracts.host_decision", side_effect=deny_success):
                    self.assertFalse(build_confirmed(task, actions(), lambda _: None))
                self.assertIn((HostOnlyDecision.TASK_SUCCESS, True), decisions)
                self.assertEqual(task.data["state"], "failed")
                self.assertTrue(task.data["report"]["host_passed"])
                self.assertFalse(task.data["report"]["pipeline_approved"])
                self.assertFalse(task.data["report"]["approved"])
                self.assertEqual(len(task.data["report"]["results"]), 3)

    def test_conversation_confirmation_does_not_bypass_ast_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                source = "import os\ndef greeting(name):\n    return 'Hallo ' + name\n"
                attempt = [
                    {"action": "write_file", "path": "agent/hello.py", "content": source},
                    {"action": "finish", "passed": True, "summary": "built", "findings": []},
                    {"action": "run_check"},
                    {"action": "finish", "passed": False, "summary": "unsafe", "findings": ["import"]},
                ]
                responses = iter(attempt * 2)
                with patch("src.forge_runtime.task_runner.exec", wraps=exec) as execute:
                    self.assertFalse(build_confirmed(task, lambda _: json.dumps(next(responses)), lambda _: None))
                execute.assert_not_called()
                self.assertEqual(task.data["state"], "failed")
                self.assertFalse(task.data["report"]["host_passed"])
                self.assertIn("exactly one function", task.data["report"]["host_detail"])
                self.assertEqual((Path(folder) / "build/agent/hello.py").read_text(encoding="utf-8"), source)
