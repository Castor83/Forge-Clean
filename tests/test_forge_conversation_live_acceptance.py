import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_conversation_live as live
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_protocol import answer, proposal


class ConversationLiveAcceptanceTests(unittest.TestCase):
    def run_greeting(self, directory, expression):
        value = proposal()
        value["objective"] = "Hallo, <Name>!; nur die leere Zeichenkette ergibt Bitte Namen eingeben."
        value["checks"][0]["cases"] = [
            {"args": ["Ada"], "expected": "Hallo, Ada!"},
            {"args": ["Bruno"], "expected": "Hallo, Bruno!"},
            {"args": [""], "expected": "Bitte Namen eingeben."}]
        actions = iter([
            {"action": "write_file", "path": "agent/hello.py", "content": "def greeting(name):\n    return " + expression + "\n"},
            {"action": "finish", "passed": True, "summary": "gebaut", "findings": []},
            {"action": "run_check"},
            {"action": "finish", "passed": True, "summary": "geprüft", "findings": []},
            {"action": "run_check"},
            {"action": "finish", "passed": True, "summary": "abgenommen", "findings": []}])
        def client(*args, **kwargs):
            return (lambda _: answer(value)) if kwargs.get("response_kind") == "conversation" else (lambda _: json.dumps(next(actions)))
        with patch.object(live, "ROOT", directory.parent.parent), patch.object(live, "LMStudioChatClient", side_effect=client), patch("builtins.print"):
            return live.main(["--workspace", str(directory), "--scenario", "greeting", "--direct"])

    def test_extra_user_case_revokes_live_success_and_preserves_built_file(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "workspaces" / "task"
            code = "(name.strip() and 'Hallo, ' + name + '!') or 'Bitte Namen eingeben.'"
            self.assertEqual(self.run_greeting(directory, code), 1)
            report = json.loads((directory / "acceptance_report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["passed"])
            self.assertTrue(report["pipeline_report"]["approved"])
            self.assertFalse(report["intent_cases_passed"])
            with TaskStore(directory).exclusive() as store:
                task = ConversationTask(store)
                self.assertEqual(task.data["state"], "failed")
                self.assertFalse(task.data["report"]["approved"])
                self.assertFalse(task.data["report"]["intent_cases_passed"])
            self.assertIn(code, (directory / "build/agent/hello.py").read_text(encoding="utf-8"))

    def test_exact_greeting_preserves_whitespace_and_passes_new_session(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "workspaces" / "task"
            code = "(name and 'Hallo, ' + name + '!') or 'Bitte Namen eingeben.'"
            self.assertEqual(self.run_greeting(directory, code), 0)
            report = json.loads((directory / "acceptance_report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["passed"])
            self.assertTrue(report["intent_cases_passed"])
            with TaskStore(directory).exclusive() as store:
                self.assertEqual(ConversationTask(store).data["state"], "completed")
