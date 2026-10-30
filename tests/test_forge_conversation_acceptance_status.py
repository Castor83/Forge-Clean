import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_cli import show_status
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_build import actions, ready


class ConversationAcceptanceStatusTests(unittest.TestCase):
    def completed(self, store):
        task = ConversationTask(store)
        ready(task)
        build_confirmed(task, actions(), lambda _: None)
        return task

    def test_status_shows_failed_extra_acceptance_alongside_original_host_success(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = self.completed(store)
                task.change(state="failed", report=dict(task.data["report"], approved=False,
                            intent_cases_passed=False, intent_detail="Zusätzlicher Fall fehlgeschlagen"))
                output = []
                show_status(task, output.append)
                self.assertIn("Zustand: Fehlgeschlagen", output)
                self.assertIn("Zusätzliche Benutzerfälle: nicht bestanden", output)
                self.assertIn("Abnahmefehler: Zusätzlicher Fall fehlgeschlagen", output)
                self.assertIn("Frühere Rollenprüfungen:", output)

    def test_non_boolean_extra_acceptance_cannot_be_saved_as_truthy_text(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = self.completed(store)
                old = store.path.read_bytes()
                with self.assertRaises(ValueError):
                    task.change(report=dict(task.data["report"], intent_cases_passed="false"))
                self.assertEqual(store.path.read_bytes(), old)

    def test_completed_state_cannot_override_failed_extra_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = self.completed(store)
                old = store.path.read_bytes()
                with self.assertRaises(ValueError):
                    task.change(report=dict(task.data["report"], intent_cases_passed=False))
                self.assertEqual(store.path.read_bytes(), old)

    def test_contradictory_persisted_completion_is_rejected_when_reopened(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                self.completed(store)
            data = json.loads(store.path.read_text(encoding="utf-8"))
            data["report"]["intent_cases_passed"] = False
            content = json.dumps(data)
            store.path.write_text(content, encoding="utf-8")
            with store.exclusive(), self.assertRaises(ValueError):
                ConversationTask(store)
            self.assertEqual(store.path.read_text(encoding="utf-8"), content)
