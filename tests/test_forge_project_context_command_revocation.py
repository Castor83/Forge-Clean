"""Malformed explicit changes cannot retain a previous build confirmation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_cli import handle_project_command
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.project_session import REVOCATION_FILENAME, update_project
from tests.test_forge_conversation_build import ready


class ProjectContextCommandRevocationTests(unittest.TestCase):
    def test_invalid_change_commands_revoke_old_proposal_and_confirmation_before_rejection(self):
        commands = ("/regel stil", "/entscheidung unbekannt Neues Ziel", "/aufgabe test unbekannt Neue Aufgabe",
                    "/naechster", "/nächster", "/projekt_anlegen", "/projekt_anlegen anderes_projekt")
        for command in commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                with TaskStore(directory).exclusive() as store:
                    task = ConversationTask(store)
                    task.initialize_project("command_contract")
                    ready(task)
                    original_context = (directory / "project_context.json").read_bytes()
                    with self.assertRaises(ValueError):
                        handle_project_command(task, command, lambda _: None)
                    self.assertFalse(task.is_confirmed())
                    self.assertIsNone(task.data["proposal"])
                    self.assertIsNone(task.data["confirmed_context_sha256"])
                    self.assertEqual((directory / "project_context.json").read_bytes(), original_context)
                    with self.assertRaises(ValueError):
                        task.confirm()
                    with self.assertRaises(ValueError):
                        build_confirmed(task, lambda _: self.fail("old command reached model"), lambda _: None)
                with TaskStore(directory).exclusive() as reopened:
                    self.assertFalse(ConversationTask(reopened).is_confirmed())

    def test_failed_revocation_save_preserves_lock_and_durable_revocation_after_restart(self):
        for entry in ("command", "api"):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                with TaskStore(directory).exclusive() as store:
                    task = ConversationTask(store)
                    task.initialize_project("command_contract")
                    ready(task)
                    original_task = store.path.read_bytes()
                    with patch.object(store, "save", side_effect=OSError("injected before revocation save")):
                        with self.assertRaises(OSError):
                            if entry == "command":
                                handle_project_command(task, "/regel stil", lambda _: None)
                            else:
                                update_project(task, "rules", "style", "Neuer Wunsch.")
                    self.assertEqual(store.path.read_bytes(), original_task)
                    self.assertTrue(store.project_uncertain)
                    self.assertTrue((directory / REVOCATION_FILENAME).is_file())
                    with self.assertRaises(ValueError):
                        build_confirmed(task, lambda _: self.fail("old persisted build reached model"), lambda _: None)
                self.assertTrue(store.lock_path.exists())
                # Only this known stopped, isolated fixture has its lock removed.
                store.lock_path.unlink()
                with TaskStore(directory).exclusive() as reopened:
                    with self.assertRaisesRegex(ValueError, "widerrufen"):
                        ConversationTask(reopened)


if __name__ == "__main__":
    unittest.main()
