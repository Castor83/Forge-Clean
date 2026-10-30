import copy
import tempfile
import threading
import unittest
from pathlib import Path

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.tool_loop import ToolLoopDeadlineExceeded
from tests.test_forge_conversation_build import ready
from tests.test_forge_conversation_protocol import answer, proposal


class ConversationDeadlineTests(unittest.TestCase):
    def test_late_response_cannot_restore_readiness_or_save_proposal(self):
        release, finished = threading.Event(), threading.Event()
        def slow_chat(messages):
            release.wait(2)
            finished.set()
            return answer(proposal())
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                try:
                    with self.assertRaises(ToolLoopDeadlineExceeded) as caught:
                        discuss(task, "Andere Begrüßung", slow_chat, max_call_seconds=0.02)
                    after_timeout = copy.deepcopy(task.data)
                    self.assertEqual(task.data["state"], "clarifying")
                    self.assertIsNone(task.data["proposal"])
                    self.assertEqual(caught.exception.role, "conversation")
                    release.set()
                    self.assertTrue(finished.wait(1))
                    self.assertEqual(task.data, after_timeout)
                    self.assertEqual(store.load(), after_timeout)
                    with self.assertRaises(ValueError):
                        build_confirmed(task, lambda _: self.fail("build called"))
                finally:
                    release.set()

    def test_oversize_new_instruction_revokes_previous_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                with self.assertRaisesRegex(ValueError, "nicht gespeichert"):
                    discuss(task, "x" * 8001, lambda _: self.fail("model called"))
                self.assertEqual(task.data["state"], "clarifying")
                self.assertIsNone(task.data["proposal"])
                with self.assertRaises(ValueError):
                    task.confirm()

    def test_full_context_revokes_previous_confirmation_without_model_call(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                task.change(messages=[{"role": "user", "content": "x" * 24000}])
                with self.assertRaisesRegex(ValueError, "Kontext|kontext"):
                    discuss(task, "neuer Wunsch", lambda _: self.fail("model called"))
                self.assertEqual(task.data["state"], "clarifying")
                self.assertIsNone(task.data["proposal"])

    def test_invalid_deadline_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                for value in (True, 0, -1, float("nan"), float("inf"), "300"):
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        discuss(task, "Wunsch", lambda _: answer(proposal()), max_call_seconds=value)
