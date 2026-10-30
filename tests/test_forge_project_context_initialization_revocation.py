"""Direct project initialization shares the host-command revocation contract."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.project_session import REVOCATION_FILENAME
from tests.test_forge_conversation_build import actions, ready


class ProjectContextInitializationRevocationTests(unittest.TestCase):
    def assert_old_proposal_blocked(self, task):
        self.assertEqual(task.data["state"], "clarifying")
        self.assertIsNone(task.data["proposal"])
        self.assertIsNone(task.data["confirmed_proposal_sha256"])
        if task.data["version"] == 2:
            self.assertIsNone(task.data["confirmed_context_sha256"])
        self.assertFalse(task.is_confirmed())
        with self.assertRaises(ValueError):
            task.confirm()
        with self.assertRaises(ValueError):
            build_confirmed(task, lambda _: self.fail("Old proposal reached a builder"), lambda _: None)

    def test_bound_ready_initialization_with_another_id_revokes_without_changing_project(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with TaskStore(directory).exclusive() as store:
                task = ConversationTask(store)
                task.initialize_project("original_project")
                ready(task)
                binding = copy.deepcopy(task.data["context_binding"])
                context = (directory / "project_context.json").read_bytes()
                marker = (directory / "project_context.binding.json").read_bytes()
                with self.assertRaisesRegex(ValueError, "bereits"):
                    task.initialize_project("other_project")
                self.assert_old_proposal_blocked(task)
                self.assertEqual(task.data["context_binding"], binding)
                self.assertEqual((directory / "project_context.json").read_bytes(), context)
                self.assertEqual((directory / "project_context.binding.json").read_bytes(), marker)
                self.assertFalse((directory / REVOCATION_FILENAME).exists())
            with TaskStore(directory).exclusive() as reopened:
                self.assert_old_proposal_blocked(ConversationTask(reopened))

    def test_legacy_ready_initialization_with_invalid_id_revokes_before_validation(self):
        for project_id in ("invalid id", "../outside", "", None, 1):
            with self.subTest(project_id=project_id), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                with TaskStore(directory).exclusive() as store:
                    task = ConversationTask(store)
                    ready(task)
                    with self.assertRaises(ValueError):
                        task.initialize_project(project_id)
                    self.assert_old_proposal_blocked(task)
                    self.assertEqual(task.data["version"], 1)
                    self.assertFalse((directory / "project_context.json").exists())
                    self.assertFalse((directory / "project_context.binding.json").exists())
                    self.assertFalse((directory / REVOCATION_FILENAME).exists())
                with TaskStore(directory).exclusive() as reopened:
                    self.assert_old_proposal_blocked(ConversationTask(reopened))

    def test_running_completed_and_cancelled_initialization_preserves_all_existing_files(self):
        for state in ("running", "completed", "cancelled"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                with TaskStore(directory).exclusive() as store:
                    task = ConversationTask(store)
                    task.initialize_project("closed_project")
                    ready(task)
                    if state == "completed":
                        self.assertTrue(build_confirmed(task, actions(), lambda _: None))
                    elif state == "cancelled":
                        task.cancel()
                    else:
                        task.change(state="running", event="Isolated running-state fixture.")
                    before_data = copy.deepcopy(task.data)
                    before_files = {path.relative_to(directory).as_posix(): path.read_bytes()
                                    for path in directory.rglob("*") if path.is_file()}
                    with self.assertRaises(ValueError):
                        task.initialize_project("invalid id")
                    after_files = {path.relative_to(directory).as_posix(): path.read_bytes()
                                   for path in directory.rglob("*") if path.is_file()}
                    self.assertEqual(task.data, before_data)
                    self.assertEqual(after_files, before_files)
                    self.assertFalse(store.project_uncertain)
                    self.assertFalse(store.retain_lock_on_close)

    def test_first_revocation_write_failure_for_valid_legacy_project_retains_durable_block(self):
        for after in (False, True):
            for error_type in (OSError, KeyboardInterrupt):
                with self.subTest(after=after, error=error_type), tempfile.TemporaryDirectory() as folder:
                    directory = Path(folder)
                    with TaskStore(directory).exclusive() as store:
                        task = ConversationTask(store)
                        ready(task)
                        original = store.path.read_bytes()
                        save = store.save

                        def failed_save(data):
                            if after:
                                save(data)
                            raise error_type("Injected initial revocation write failure")

                        with patch.object(store, "save", side_effect=failed_save):
                            with self.assertRaises(error_type):
                                task.initialize_project("requested_project")
                        if after:
                            self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["state"],
                                             "clarifying")
                        else:
                            self.assertEqual(store.path.read_bytes(), original)
                        self.assertTrue(store.project_uncertain)
                        self.assertTrue(store.retain_lock_on_close)
                        revocation = directory / REVOCATION_FILENAME
                        self.assertEqual(json.loads(revocation.read_text(encoding="utf-8"))["project_id"],
                                         "requested_project")
                        self.assertFalse((directory / "project_context.json").exists())
                        with self.assertRaises(ValueError):
                            build_confirmed(task, lambda _: self.fail("Failed initialization reached builder"),
                                            lambda _: None)
                    self.assertTrue(store.lock_path.exists())
                    # Only this stopped isolated fixture removes its known lock.
                    store.lock_path.unlink()
                    with TaskStore(directory).exclusive() as reopened:
                        with self.assertRaisesRegex(ValueError, "widerrufen"):
                            ConversationTask(reopened)


if __name__ == "__main__":
    unittest.main()
