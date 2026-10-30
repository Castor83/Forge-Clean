import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_protocol import discuss, parse_conversation_response
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_protocol import answer, proposal


class ConversationJsonTests(unittest.TestCase):
    def test_deep_model_response_gets_one_correction_without_crashing(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                responses = iter(["[" * 14000 + "0" + "]" * 14000, answer(proposal())])
                response = discuss(task, "Hallo mit Name", lambda _: next(responses))
                self.assertEqual(response["task"], proposal())
                self.assertEqual(task.data["state"], "proposed")
                self.assertFalse(task.is_confirmed())

    def test_duplicate_model_field_is_not_silently_overwritten(self):
        with self.assertRaisesRegex(ValueError, "mehrfach"):
            parse_conversation_response('{"reply":"eins","reply":"zwei","questions":["Frage?"],"task":null}')

    def test_duplicate_persisted_field_is_rejected_and_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            content = '{"state":"ready","state":"clarifying"}'
            store.path.write_text(content, encoding="utf-8")
            with store.exclusive():
                with self.assertRaisesRegex(ValueError, "mehrfach"):
                    ConversationTask(store)
            self.assertEqual(store.path.read_text(encoding="utf-8"), content)
            self.assertFalse(store.lock_path.exists())

    def test_deep_persisted_data_is_rejected_without_leaving_a_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            content = "[" * 14000 + "0" + "]" * 14000
            store.path.write_text(content, encoding="utf-8")
            with store.exclusive():
                with self.assertRaisesRegex(ValueError, "verschachtelt"):
                    ConversationTask(store)
            self.assertEqual(store.path.read_text(encoding="utf-8"), content)
            self.assertFalse(store.lock_path.exists())

    def test_nonfinite_model_number_is_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), self.assertRaisesRegex(ValueError, "nicht endliche"):
                parse_conversation_response('{"reply":"Antwort","questions":[],"task":' + constant + '}')
