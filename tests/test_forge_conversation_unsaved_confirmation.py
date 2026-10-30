import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_cli import interact
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_build import ready
from tests.test_forge_conversation_protocol import answer, proposal


class ConversationUnsavedConfirmationTests(unittest.TestCase):
    def test_failed_save_of_new_instruction_blocks_old_build_and_next_session(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                old = store.path.read_bytes()
                with patch("src.forge_runtime.conversation_store.os.replace", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        discuss(task, "Andere Ausgabe bitte", lambda _: self.fail("Model called"))
                self.assertFalse(task.is_confirmed())
                self.assertEqual(task.data["state"], "clarifying")
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda _: self.fail("Old build called"))
                self.assertEqual(store.path.read_bytes(), old)
            self.assertTrue(store.lock_path.exists())
            with self.assertRaises(RuntimeError):
                with TaskStore(Path(folder)).exclusive():
                    self.fail("Unresolved saved confirmation was reopened")
            self.assertEqual(store.path.read_bytes(), old)

    def test_retry_saves_new_instruction_and_releases_retained_lock_normally(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                with patch("src.forge_runtime.conversation_store.os.replace", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        discuss(task, "Andere Ausgabe", lambda _: None)
                value = proposal()
                value["objective"] = "Neu geprüfter Auftrag"
                discuss(task, "Andere Ausgabe erneut", lambda _: answer(value))
                self.assertEqual(task.data["state"], "proposed")
                self.assertFalse(task.is_confirmed())
                task.confirm()
            self.assertFalse(store.lock_path.exists())
            with store.exclusive():
                task = ConversationTask(store)
                self.assertTrue(task.is_confirmed())
                self.assertEqual(task.data["proposal"]["objective"], "Neu geprüfter Auftrag")

    def test_terminal_discloses_failed_storage_and_exits_unsuccessfully(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                inputs = iter(["Andere Ausgabe", "/bauen", "/ende"])
                output = []
                with patch("src.forge_runtime.conversation_store.os.replace", side_effect=OSError("disk full")):
                    code = interact(task, lambda _: self.fail("Model called"), lambda _: self.fail("Build called"), lambda _: next(inputs), output.append)
                self.assertEqual(code, 1)
                self.assertTrue(any("nicht gespeichert" in line for line in output))
                self.assertTrue(any("Sperrdatei bleibt erhalten" in line for line in output))
            self.assertTrue(store.lock_path.exists())

    def test_reloading_in_same_failed_session_cannot_restore_old_readiness(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                ready(task)
                with patch("src.forge_runtime.conversation_store.os.replace", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        discuss(task, "Änderung", lambda _: None)
                    with self.assertRaises(OSError):
                        ConversationTask(store)

    def test_successfully_saved_cancellation_releases_lock_without_old_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                with patch("src.forge_runtime.conversation_store.os.replace", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        discuss(task, "Änderung", lambda _: None)
                task.cancel()
                self.assertFalse(task.is_confirmed())
            self.assertFalse(store.lock_path.exists())
            with store.exclusive():
                self.assertEqual(ConversationTask(store).data["state"], "cancelled")
