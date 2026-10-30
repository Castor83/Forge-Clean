import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_cli import interact
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_build import ready


class ConversationStorageInterruptTests(unittest.TestCase):
    def test_keyboard_interrupt_during_save_revokes_confirmation_and_retains_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                old = store.path.read_bytes()
                with patch("src.forge_runtime.conversation_store.os.replace", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        discuss(task, "Ändere den Auftrag", lambda _: self.fail("Model called"))
                self.assertFalse(task.is_confirmed())
                self.assertEqual(store.path.read_bytes(), old)
                self.assertEqual(list(Path(folder).glob("*.tmp")), [])
            self.assertTrue(store.lock_path.exists())
            with self.assertRaises(RuntimeError):
                with TaskStore(Path(folder)).exclusive():
                    self.fail("Interrupted confirmation was reopened")

    def test_terminal_reports_unresolved_storage_interrupt_instead_of_direct_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                output = []
                with patch("src.forge_runtime.conversation_store.os.replace", side_effect=KeyboardInterrupt):
                    code = interact(task, lambda _: self.fail("Model called"), None,
                                    lambda _: "Ändere den Auftrag", output.append)
                self.assertEqual(code, 130)
                self.assertTrue(any("Speicherstand" in line for line in output))
                self.assertTrue(any("Sperrdatei bleibt erhalten" in line for line in output))
                self.assertFalse(any("kann erneut geöffnet" in line for line in output))
            self.assertTrue(store.lock_path.exists())

    def test_interrupt_while_preparing_new_state_also_revokes_old_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                old = store.path.read_bytes()
                with patch("src.forge_runtime.conversation_store.copy.deepcopy", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        discuss(task, "Anderer Auftrag", lambda _: self.fail("Model called"))
                self.assertFalse(task.is_confirmed())
                self.assertEqual(task.data["state"], "clarifying")
                self.assertEqual(store.path.read_bytes(), old)
            self.assertTrue(store.lock_path.exists())
