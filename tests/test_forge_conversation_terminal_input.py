import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_cli import interact
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_protocol import answer, proposal


class ConversationTerminalInputTests(unittest.TestCase):
    def test_user_answer_reaches_model_and_saved_history_verbatim(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                discuss(task, "Welcher Name?", lambda _: answer(questions=["Wie lautet der genaue Name?"]))
                message = "  Ada \t"
                inputs = iter([message, "/ende"])
                seen = []
                def planner(messages):
                    seen.append(messages)
                    return answer(proposal())
                self.assertEqual(interact(task, planner, None, lambda _: next(inputs), lambda _: None), 0)
                self.assertEqual(seen[0][-1], ("user", message))
                self.assertEqual(task.data["messages"][-2]["content"], message)
            with store.exclusive():
                self.assertEqual(ConversationTask(store).data["messages"][-2]["content"], message)

    def test_padded_commands_still_work_without_model_call(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                discuss(task, "Name", lambda _: answer(proposal()))
                inputs = iter(["  /bestätigen  ", " \t/status  ", "  /ende  "])
                self.assertEqual(interact(task, lambda _: self.fail("Model called"), None, lambda _: next(inputs), lambda _: None), 0)
                self.assertTrue(task.is_confirmed())

    def test_blank_line_is_ignored_without_revoking_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                discuss(task, "Name", lambda _: answer(proposal()))
                task.confirm()
                count = len(task.data["messages"])
                inputs = iter([" \t ", "/ende"])
                self.assertEqual(interact(task, lambda _: self.fail("Model called"), None, lambda _: next(inputs), lambda _: None), 0)
                self.assertTrue(task.is_confirmed())
                self.assertEqual(len(task.data["messages"]), count)

    def test_oversize_raw_input_is_not_shortened_to_bypass_message_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                discuss(task, "Name", lambda _: answer(proposal()))
                task.confirm()
                inputs = iter([" " * 8000 + "Ada", "/ende"])
                output = []
                self.assertEqual(interact(task, lambda _: self.fail("Model called"), None, lambda _: next(inputs), output.append), 0)
                self.assertFalse(task.is_confirmed())
                self.assertEqual(task.data["state"], "clarifying")
                self.assertTrue(any("nicht gespeichert" in line for line in output))
