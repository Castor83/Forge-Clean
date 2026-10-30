import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_store import ConversationTask, TaskStore


class ConversationStoreTests(unittest.TestCase):
    def test_round_trip_and_confirmation_requires_proposal(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                with self.assertRaises(ValueError):
                    task.confirm()
                task.change(state="proposed", proposal={"objective": "test"})
                task.confirm()
            with store.exclusive():
                self.assertEqual(ConversationTask(store).data["state"], "ready")

    def test_second_process_cannot_open_same_task(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                with self.assertRaisesRegex(RuntimeError, "gesperrt"):
                    with TaskStore(Path(folder)).exclusive():
                        self.fail("Concurrent session opened")
            self.assertFalse(store.lock_path.exists())

    def test_running_state_becomes_interrupted_on_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                task.change(state="running", proposal={"objective": "test"})
            with store.exclusive():
                task = ConversationTask(store)
                self.assertEqual(task.data["state"], "interrupted")
                with self.assertRaises(ValueError):
                    task.confirm()

    def test_failed_atomic_replace_preserves_previous_state(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                task.change(event="gespeichert")
                old = store.path.read_bytes()
                with patch("src.forge_runtime.conversation_store.os.replace", side_effect=OSError("disk")):
                    with self.assertRaises(OSError):
                        task.cancel()
                self.assertEqual(store.path.read_bytes(), old)
                self.assertEqual(task.data["state"], "clarifying")
                self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_invalid_state_and_oversize_history_are_not_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                with self.assertRaises(ValueError):
                    task.change(state="completed")
                with self.assertRaises(ValueError):
                    task.change(messages=[{"role": "user", "content": "x" * 1_000_000}])
                self.assertFalse(store.path.exists())

    def test_unknown_version_and_stale_lock_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                task.change(event="start")
                data = json.loads(store.path.read_text(encoding="utf-8"))
                data["version"] = 2
                store.path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    store.load()
            store.lock_path.write_text("999999", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                with store.exclusive():
                    self.fail("Stale lock ignored")
            self.assertTrue(store.lock_path.exists())
