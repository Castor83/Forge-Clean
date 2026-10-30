import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_protocol import proposal


class ConversationCorruptionTests(unittest.TestCase):
    def test_malformed_failed_report_is_rejected_before_display(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                task.change(state="proposed", proposal=proposal())
                for report in ({"results": "not a list"}, {"host_passed": 1}, {"results": ["fake"]}):
                    with self.subTest(report=report), self.assertRaises(ValueError):
                        task.change(state="failed", report=report)
                self.assertEqual(task.data["state"], "proposed")

    def test_false_or_incomplete_persisted_success_is_rejected_on_load(self):
        reports = [None, {"approved": True, "host_passed": False, "results": []},
                   {"approved": True, "host_passed": True, "results": [
                       {"role": role, "passed": role != "reviewer", "summary": "test", "findings": []}
                       for role in ("builder", "tester", "reviewer")]},
                   {"approved": True, "host_passed": True, "results": [
                       {"role": "builder", "passed": True, "summary": "test", "findings": []}
                       for _ in range(3)]}]
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                task.change(state="proposed", proposal=proposal())
                original = store.path.read_bytes()
                for report in reports:
                    with self.subTest(report=report):
                        data = json.loads(original)
                        data.update(state="completed", report=report)
                        store.path.write_text(json.dumps(data), encoding="utf-8")
                        with self.assertRaises(ValueError):
                            store.load()
