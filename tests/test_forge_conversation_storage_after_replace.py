import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_cli import interact
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_build import ready


class ConversationStorageAfterReplaceTests(unittest.TestCase):
    def test_interrupt_after_actual_replace_keeps_build_blocked_without_claiming_old_file(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            replace = os.replace
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)

                def interrupted_replace(source, destination):
                    replace(source, destination)
                    raise KeyboardInterrupt

                with patch("src.forge_runtime.conversation_store.os.replace", side_effect=interrupted_replace):
                    with self.assertRaises(KeyboardInterrupt):
                        discuss(task, "Neuer Wunsch", lambda _: self.fail("Model called"))
                saved = json.loads(store.path.read_text(encoding="utf-8"))
                self.assertEqual(saved["state"], "clarifying")
                self.assertEqual(saved["messages"][-1]["content"], "Neuer Wunsch")
                self.assertFalse(task.is_confirmed())
                self.assertIn("ungeklärt", task.data["questions"][0])
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda _: self.fail("Old build called"))
            self.assertTrue(store.lock_path.exists())
            with self.assertRaises(RuntimeError):
                with TaskStore(Path(folder)).exclusive():
                    self.fail("Unresolved storage was reopened")

    def test_terminal_reports_uncertain_storage_even_when_replacement_already_happened(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            replace = os.replace
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                output = []
                inputs = iter(["Anderer Wunsch", "/ende"])

                def failed_replace(source, destination):
                    replace(source, destination)
                    raise OSError("failure reported after replacement")

                with patch("src.forge_runtime.conversation_store.os.replace", side_effect=failed_replace):
                    code = interact(task, lambda _: self.fail("Model called"), None,
                                    lambda _: next(inputs), output.append)
                self.assertEqual(code, 1)
                self.assertTrue(any("Speicherstand ungeklärt" in line for line in output))
                saved = json.loads(store.path.read_text(encoding="utf-8"))
                self.assertEqual(saved["messages"][-1]["content"], "Anderer Wunsch")
                self.assertFalse(task.is_confirmed())
            self.assertTrue(store.lock_path.exists())
