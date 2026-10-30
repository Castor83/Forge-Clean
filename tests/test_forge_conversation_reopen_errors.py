import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_build import build_confirmed, check_completed, prepare_resume
from src.forge_runtime.conversation_cli import interact
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_build import actions, ready


class ConversationReopenErrorTests(unittest.TestCase):
    def test_unexpected_file_revokes_completion_but_keeps_terminal_available(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                build_confirmed(task, actions(), lambda _: None)
            extra = Path(folder) / "build/user-note.txt"
            extra.write_text("Benutzerdatei erhalten", encoding="utf-8")
            with store.exclusive():
                task = ConversationTask(store)
                check_completed(task)
                self.assertEqual(task.data["state"], "failed")
                self.assertFalse(task.data["report"]["host_passed"])
                self.assertIn("user-note.txt", task.data["report"]["error"])
                inputs = iter(["/status", "/wiederaufnahme", "/ende"])
                output = []
                self.assertEqual(interact(task, None, None, lambda _: next(inputs), output.append), 0)
                self.assertTrue(any("Nicht ausgeführt" in line for line in output))
                self.assertEqual(extra.read_text(encoding="utf-8"), "Benutzerdatei erhalten")
                self.assertEqual(task.data["state"], "failed")

    def test_read_access_failure_is_recorded_without_false_success(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                build_confirmed(task, actions(), lambda _: None)
                with patch("src.forge_runtime.conversation_build.inventory", side_effect=PermissionError("Zugriff verweigert")):
                    check_completed(task)
                self.assertEqual(task.data["state"], "failed")
                self.assertFalse(task.data["report"]["host_passed"])
                self.assertIn("PermissionError", task.data["report"]["error"])
                with self.assertRaises(ValueError):
                    task.confirm()

    def test_after_user_resolves_extra_file_resume_requires_new_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                build_confirmed(task, actions(), lambda _: None)
                extra = Path(folder) / "build/user-note.txt"
                extra.write_text("note", encoding="utf-8")
                check_completed(task)
                extra.rename(Path(folder) / "user-note.txt")
                files, passed, _ = prepare_resume(task)
                self.assertTrue(passed)
                self.assertIn("agent/hello.py", files)
                self.assertFalse(task.is_confirmed())
                task.confirm()
                self.assertTrue(build_confirmed(task, actions(), lambda _: None))
                self.assertEqual((Path(folder) / "user-note.txt").read_text(), "note")
