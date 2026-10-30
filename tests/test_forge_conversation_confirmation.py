import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_store import ConversationTask, TaskStore, proposal_digest
from tests.test_forge_conversation_build import ready
from tests.test_forge_conversation_protocol import proposal


class ConversationConfirmationTests(unittest.TestCase):
    def test_unchanged_confirmed_proposal_survives_new_session(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                digest = task.data["confirmed_proposal_sha256"]
            with store.exclusive():
                task = ConversationTask(store)
                self.assertTrue(task.is_confirmed())
                self.assertEqual(task.data["confirmed_proposal_sha256"], digest)

    def test_external_proposal_edit_invalidates_saved_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
            data = json.loads(store.path.read_text(encoding="utf-8"))
            data["proposal"]["checks"][0]["cases"][0]["expected"] = "Andere Ausgabe"
            store.path.write_text(json.dumps(data), encoding="utf-8")
            with store.exclusive():
                task = ConversationTask(store)
                self.assertEqual(task.data["state"], "proposed")
                self.assertFalse(task.is_confirmed())
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda _: self.fail("model called"))
                self.assertFalse((Path(folder) / "build").exists())

    def test_in_memory_edit_is_blocked_before_build(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                task.data["proposal"]["objective"] = "Unbestätigte Erweiterung"
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda _: self.fail("model called"))
                self.assertEqual(task.data["state"], "proposed")
                self.assertIsNone(task.data["confirmed_proposal_sha256"])

    def test_legacy_ready_state_requires_fresh_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
            data = json.loads(store.path.read_text(encoding="utf-8"))
            data.pop("confirmed_proposal_sha256")
            store.path.write_text(json.dumps(data), encoding="utf-8")
            with store.exclusive():
                task = ConversationTask(store)
                self.assertEqual(task.data["state"], "proposed")
                task.confirm()
                self.assertTrue(task.is_confirmed())

    def test_cosmetic_json_order_does_not_change_confirmation(self):
        value = proposal()
        reordered = dict(reversed(list(value.items())))
        self.assertEqual(proposal_digest(value), proposal_digest(reordered))
