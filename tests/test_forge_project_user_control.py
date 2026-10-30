"""User removal preserves host gates, unrelated data and restart revocation."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime import project_session
from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_cli import handle_project_command, interact
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.project_context import CONTEXT_FILENAME, validate_project_context
from src.forge_runtime.project_control import remove_project_record
from src.forge_runtime.project_session import REVOCATION_FILENAME, update_project
from tests.test_forge_conversation_build import actions, ready
from tests.test_forge_conversation_protocol import answer


class ProjectUserControlTests(unittest.TestCase):
    def prepare(self, task):
        task.initialize_project("user_control")
        update_project(task, "rules", "style", "ENTFERNTER_REGELTEXT_ALT")
        update_project(task, "rules", "style", "ENTFERNTER_REGELTEXT_NEU")
        update_project(task, "rules", "other", "Andere Regel erhalten.")
        update_project(task, "decisions", "objective", "ENTFERNTES_ZIEL_ALT")
        update_project(task, "decisions", "objective", "ENTFERNTES_ZIEL_NEU")
        update_project(task, "decisions", "working_notes", "Arbeitsweise erhalten.")
        ready(task)
        task.change(messages=[{"role": "user", "content": "ENTFERNTE_ALTE_NACHRICHT"},
                              {"role": "assistant", "content": "ENTFERNTE_ALTE_ANTWORT"}])

    def snapshot(self, directory):
        return {path.relative_to(directory).as_posix(): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def test_removal_purges_all_revisions_and_only_selected_project_record(self):
        for field, key, command in (("rules", "style", "/regel_entfernen style"),
                                    ("decisions", "objective", "/entscheidung_entfernen ziel")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                unrelated = directory / "user_note.txt"
                unrelated.write_bytes(b"unrelated user data")
                historical = directory / "raw_dialogue.log"
                historical.write_bytes(b"historical raw response")
                with TaskStore(directory).exclusive() as store:
                    task = ConversationTask(store)
                    self.prepare(task)
                    before = copy.deepcopy(task.project_data())
                    output = []
                    self.assertTrue(handle_project_command(task, command, output.append))
                    after = task.project_data()
                    identifier = "id" if field == "rules" else "key"
                    expected = copy.deepcopy(before)
                    expected[field] = [record for record in expected[field] if record[identifier] != key]
                    expected["revision"] += 1
                    self.assertEqual(after, expected)
                    self.assertEqual(task.data["messages"], [])
                    self.assertIsNone(task.data["proposal"])
                    self.assertIsNone(task.data["report"])
                    self.assertEqual(task.data["state"], "clarifying")
                    self.assertFalse(task.is_confirmed())
                    self.assertIsNone(task.data["confirmed_context_sha256"])
                    self.assertIsNone(task.data["confirmed_proposal_sha256"])
                    self.assertIn("Historische Nachweise und andere Dateien bleiben erhalten.", "\n".join(output))
                    self.assertEqual(json.loads(store.path.read_text(encoding="utf-8")), task.data)
                    self.assertEqual(json.loads((directory / CONTEXT_FILENAME).read_text(encoding="utf-8")), expected)
                    with self.assertRaises(ValueError):
                        build_confirmed(task, lambda _: self.fail("old proposal reached model"), lambda _: None)
                with TaskStore(directory).exclusive() as reopened:
                    task = ConversationTask(reopened)
                    self.assertEqual(task.project_data(), expected)
                    self.assertEqual(task.data["messages"], [])
                    self.assertFalse(task.is_confirmed())
                self.assertEqual(unrelated.read_bytes(), b"unrelated user data")
                self.assertEqual(historical.read_bytes(), b"historical raw response")

    def test_last_rule_or_decision_can_be_removed_without_schema_change(self):
        for field, key in (("rules", "only"), ("decisions", "objective")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                with TaskStore(Path(folder)).exclusive() as store:
                    task = ConversationTask(store)
                    task.initialize_project("user_control")
                    update_project(task, field, key, "Nur dieser Eintrag.")
                    remove_project_record(task, field, key)
                    context = task.project_data()
                    validate_project_context(context)
                    self.assertEqual(context["version"], 1)
                    self.assertEqual(context["rules"], [])
                    self.assertEqual(context["decisions"], [])

    def test_removed_context_and_old_conversation_do_not_reach_next_model_call(self):
        for field, key, marker in (("rules", "style", "ENTFERNTER_REGELTEXT"),
                                   ("decisions", "objective", "ENTFERNTES_ZIEL")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                with TaskStore(directory).exclusive() as store:
                    task = ConversationTask(store)
                    self.prepare(task)
                    remove_project_record(task, field, key)
                with TaskStore(directory).exclusive() as reopened:
                    task = ConversationTask(reopened)
                    seen = []

                    def planner(messages):
                        seen.extend(messages)
                        return answer(questions=["Welchen neuen Auftrag möchtest du?"])

                    discuss(task, "Neuen Auftrag klären.", planner)
                    payload = "\n".join(content for _, content in seen)
                    self.assertNotIn(marker, payload)
                    self.assertNotIn("ENTFERNTE_ALTE_NACHRICHT", payload)
                    self.assertNotIn("ENTFERNTE_ALTE_ANTWORT", payload)
                    self.assertIn("Arbeitsweise erhalten.", payload)
                    self.assertIn("Neuen Auftrag klären.", payload)
                    self.assertFalse(task.is_confirmed())

    def test_invalid_removal_commands_revoke_ready_proposal_but_preserve_context(self):
        commands = ("/regel_entfernen", "/regel_entfernen style extra",
                    "/regel_entfernen missing", "/entscheidung_entfernen",
                    "/entscheidung_entfernen unbekannt", "/entscheidung_entfernen ziel extra")
        for command in commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                with TaskStore(directory).exclusive() as store:
                    task = ConversationTask(store)
                    self.prepare(task)
                    before = (directory / CONTEXT_FILENAME).read_bytes()
                    with self.assertRaises(ValueError):
                        handle_project_command(task, command, lambda _: None)
                    self.assertFalse(task.is_confirmed())
                    self.assertIsNone(task.data["proposal"])
                    self.assertIsNone(task.data["confirmed_context_sha256"])
                    self.assertEqual((directory / CONTEXT_FILENAME).read_bytes(), before)
                    with self.assertRaises(ValueError):
                        build_confirmed(task, lambda _: self.fail("invalid removal kept approval"), lambda _: None)
                with TaskStore(directory).exclusive() as reopened:
                    self.assertFalse(ConversationTask(reopened).is_confirmed())

    def test_noneditable_states_are_rejected_without_changing_any_file(self):
        for state in ("running", "completed", "cancelled"):
            for entry in ("api", "command"):
                with self.subTest(state=state, entry=entry), tempfile.TemporaryDirectory() as folder:
                    directory = Path(folder)
                    with TaskStore(directory).exclusive() as store:
                        task = ConversationTask(store)
                        self.prepare(task)
                        if state == "completed":
                            self.assertTrue(build_confirmed(task, actions(), lambda _: None))
                        elif state == "cancelled":
                            task.cancel()
                        else:
                            task.change(state="running", event="Isolierte Laufzustandsprobe.")
                        before = self.snapshot(directory)
                        old_data = copy.deepcopy(task.data)
                        with self.assertRaises(ValueError):
                            if entry == "api":
                                remove_project_record(task, "rules", "style")
                            else:
                                handle_project_command(task, "/regel_entfernen style", lambda _: None)
                        self.assertEqual(self.snapshot(directory), before)
                        self.assertEqual(task.data, old_data)
                        self.assertFalse(store.project_uncertain)

    def test_failed_initial_revocation_save_keeps_durable_denial_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with TaskStore(directory).exclusive() as store:
                task = ConversationTask(store)
                self.prepare(task)
                original_context = (directory / CONTEXT_FILENAME).read_bytes()
                with patch.object(store, "save", side_effect=OSError("injected revocation save failure")):
                    with self.assertRaises(OSError):
                        remove_project_record(task, "rules", "style")
                self.assertTrue(store.project_uncertain)
                self.assertTrue(store.retain_lock_on_close)
                self.assertTrue((directory / REVOCATION_FILENAME).is_file())
                self.assertEqual((directory / CONTEXT_FILENAME).read_bytes(), original_context)
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda _: self.fail("failed deletion kept approval"), lambda _: None)
            self.assertTrue(store.lock_path.exists())
            # Only this stopped isolated test session has its lock removed.
            store.lock_path.unlink()
            with TaskStore(directory).exclusive() as reopened:
                with self.assertRaisesRegex(ValueError, "widerrufen"):
                    ConversationTask(reopened)

    def test_each_context_transaction_failure_or_interrupt_blocks_build_and_retains_lock(self):
        for failed_write in (1, 2, 3):
            for after in (False, True):
                for error_type in (OSError, KeyboardInterrupt):
                    with self.subTest(write=failed_write, after=after, error=error_type), \
                            tempfile.TemporaryDirectory() as folder:
                        directory = Path(folder)
                        with TaskStore(directory).exclusive() as store:
                            task = ConversationTask(store)
                            self.prepare(task)
                            original = project_session._atomic_json
                            writes = []

                            def fail(path, data, maximum):
                                writes.append(path.name)
                                if len(writes) == failed_write and not after:
                                    raise error_type("injected transaction failure")
                                original(path, data, maximum)
                                if len(writes) == failed_write and after:
                                    raise error_type("injected transaction failure after replace")

                            with patch.object(project_session, "_atomic_json", side_effect=fail):
                                with self.assertRaises(error_type):
                                    remove_project_record(task, "rules", "style")
                            self.assertTrue(store.project_uncertain)
                            self.assertTrue(store.retain_lock_on_close)
                            self.assertTrue((directory / REVOCATION_FILENAME).is_file())
                            with self.assertRaises(ValueError):
                                build_confirmed(task, lambda _: self.fail("uncertain removal reached model"), lambda _: None)
                        self.assertTrue(store.lock_path.exists())
                        store.lock_path.unlink()
                        with TaskStore(directory).exclusive() as reopened:
                            with self.assertRaisesRegex(ValueError, "widerrufen"):
                                ConversationTask(reopened)

    def test_interactive_terminal_handles_removal_without_model_or_build_call(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                self.prepare(task)
                commands = iter(("/regel_entfernen style", "/entscheidung_entfernen ziel", "/ende"))
                output = []
                deny = lambda _: self.fail("project control invoked the model")
                self.assertEqual(interact(task, deny, deny, lambda _: next(commands), output.append), 0)
                self.assertEqual(task.project_data()["rules"][0]["id"], "other")
                self.assertEqual(task.project_data()["decisions"][0]["key"], "working_notes")
                self.assertIn("/regel_entfernen", output[1])
                self.assertIn("/entscheidung_entfernen", output[1])


if __name__ == "__main__":
    unittest.main()
