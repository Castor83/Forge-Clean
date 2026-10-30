import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_protocol import answer, proposal


class ConversationCorrectionTests(unittest.TestCase):
    def test_invalid_model_proposal_gets_one_correction_without_build_permission(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                invalid = proposal()
                invalid["allowed_files"] = ["../outside.py"]
                replies = iter([answer(invalid), answer(questions=["Welche reine Textprüfung soll ich zuerst bauen?"])])
                calls = []
                def chat(messages):
                    calls.append(messages)
                    return next(replies)
                result = discuss(task, "Sortiere meine Dateien", chat)
                self.assertIsNone(result["task"])
                self.assertEqual(task.data["state"], "clarifying")
                self.assertEqual(len(calls), 2)
                feedback = json.loads(calls[1][-1][1])
                self.assertEqual(feedback["kind"], "proposal_validation_feedback")
                with self.assertRaises(ValueError):
                    task.confirm()
                self.assertFalse((Path(folder) / "build").exists())

    def test_repeated_invalid_proposals_stop_after_two_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                count = []
                def chat(messages):
                    count.append(messages)
                    return "invalid"
                with self.assertRaises(ValueError):
                    discuss(task, "Wunsch", chat)
                self.assertEqual(len(count), 2)
                self.assertEqual(task.data["state"], "clarifying")
                self.assertIsNone(task.data["proposal"])
