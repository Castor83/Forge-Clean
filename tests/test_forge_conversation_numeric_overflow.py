import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore, fresh_task
from tests.test_forge_conversation_protocol import answer, proposal


class ConversationNumericOverflowTests(unittest.TestCase):
    def test_overflow_in_saved_report_is_rejected_without_changing_the_file(self):
        for number in ("1e309", "-1e309"):
            with self.subTest(number=number), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder))
                data = fresh_task()
                data["report"] = {"host_detail": "OVERFLOW"}
                content = json.dumps(data).replace('"OVERFLOW"', number)
                store.path.write_text(content, encoding="utf-8")
                with store.exclusive():
                    with self.assertRaisesRegex(ValueError, "nicht endliche"):
                        ConversationTask(store)
                self.assertEqual(store.path.read_text(encoding="utf-8"), content)
                self.assertFalse(store.lock_path.exists())

    def test_model_overflow_receives_explicit_numeric_feedback_before_correction(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                value = proposal()
                value["checks"][0]["cases"][0]["expected"] = "OVERFLOW"
                responses = iter([answer(value).replace('"OVERFLOW"', "1e309"), answer(proposal())])
                seen = []

                def chat(messages):
                    seen.append(messages)
                    return next(responses)

                discuss(task, "Begrüße einen Namen", chat)
                self.assertEqual(len(seen), 2)
                feedback = json.loads(seen[1][-1][1])
                self.assertIn("nicht endliche", feedback["error"])
                self.assertEqual(task.data["state"], "proposed")
                self.assertEqual(task.data["proposal"], proposal())
                self.assertFalse(task.is_confirmed())

    def test_large_finite_numbers_still_build_and_survive_reopening(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                value = proposal()
                value["objective"] = "Gib die eingegebene Zahl unverändert zurück."
                value["checks"][0]["cases"] = [
                    {"args": [1e308], "expected": 1e308},
                    {"args": [-1e308], "expected": -1e308}]
                discuss(task, "Gib eine Zahl zurück", lambda _: answer(value))
                task.confirm()
                actions = iter([
                    {"action": "write_file", "path": "agent/hello.py",
                     "content": "def greeting(value):\n    return value\n"},
                    {"action": "finish", "passed": True, "summary": "Gebaut", "findings": []},
                    {"action": "run_check"},
                    {"action": "finish", "passed": True, "summary": "Geprüft", "findings": []},
                    {"action": "run_check"},
                    {"action": "finish", "passed": True, "summary": "Abgenommen", "findings": []}])
                self.assertTrue(build_confirmed(task, lambda _: json.dumps(next(actions)), lambda _: None))
            with store.exclusive():
                task = ConversationTask(store)
                self.assertEqual(task.data["state"], "completed")
                self.assertEqual(task.data["proposal"], value)
