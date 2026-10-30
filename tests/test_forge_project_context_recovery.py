"""Explicit recovery adopts valid data while permanently revoking old readiness."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from src.forge_runtime import project_session
from src.forge_runtime import conversation_cli
from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.project_context import CONTEXT_FILENAME
from src.forge_runtime.project_session import recover_project_store
from tests.test_forge_conversation_build import actions, ready
from tests.test_forge_conversation_protocol import answer, proposal


class ProjectContextRecoveryTests(unittest.TestCase):
    def bound_ready(self, store):
        task = ConversationTask(store)
        task.initialize_project("test_project")
        ready(task)
        return task

    def mutate_valid_context(self, directory):
        path = directory / CONTEXT_FILENAME
        data = json.loads(path.read_text(encoding="utf-8"))
        data["revision"] += 1
        data["next_step"] = "Ausdrücklich übernommener neuer Arbeitsstand."
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def assert_requires_new_proposal(self, task):
        self.assertEqual(task.data["version"], 2)
        self.assertEqual(task.data["state"], "clarifying")
        self.assertIsNone(task.data["proposal"])
        self.assertIsNone(task.data["confirmed_proposal_sha256"])
        self.assertIsNone(task.data["confirmed_context_sha256"])
        self.assertFalse(task.is_confirmed())
        with self.assertRaises(ValueError):
            task.confirm()
        with self.assertRaises(ValueError):
            build_confirmed(task, lambda _: self.fail("old build reached model"), lambda _: None)

    def test_explicit_recovery_adopts_valid_changed_context_without_old_build_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.bound_ready(store)
                changed = self.mutate_valid_context(store.directory)
                with self.assertRaises(ValueError):
                    task.project_data()
                self.assertTrue(store.project_uncertain)
                self.assertTrue(store.retain_lock_on_close)
                task.recover_project("test_project")
                self.assertEqual(task.project_data(), changed)
                self.assertFalse(store.project_uncertain)
                self.assertFalse(store.retain_lock_on_close)
                self.assertFalse((store.directory / project_session.REVOCATION_FILENAME).exists())
                self.assert_requires_new_proposal(task)
                seen = []

                def planner(messages):
                    seen.append(messages)
                    return answer(proposal())

                discuss(task, "Hallo mit Name erneut bauen", planner)
                self.assertEqual(len(seen), 1)
                self.assertIn(changed["next_step"], json.dumps(seen[0], ensure_ascii=False))
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda _: self.fail("unconfirmed new proposal was built"), lambda _: None)
                task.confirm()
                self.assertTrue(build_confirmed(task, actions(), lambda _: None))
                self.assertTrue(task.data["report"]["approved"])
            self.assertFalse(store.lock_path.exists())

    def test_partial_first_initialization_can_recover_with_or_without_existing_context(self):
        for after_context_write in (False, True):
            with self.subTest(after_context_write=after_context_write), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder))
                with store.exclusive():
                    task = ConversationTask(store)
                    ready(task)
                    atomic = project_session._atomic_json

                    def fail_context(path, data, maximum):
                        if path.name == CONTEXT_FILENAME:
                            if after_context_write:
                                atomic(path, data, maximum)
                            raise OSError("injected initialization context write failure")
                        return atomic(path, data, maximum)

                    with patch.object(project_session, "_atomic_json", side_effect=fail_context):
                        with self.assertRaises(OSError):
                            task.initialize_project("test_project")
                    self.assertEqual((store.directory / CONTEXT_FILENAME).exists(), after_context_write)
                    self.assertTrue(store.project_uncertain)
                    task.recover_project("test_project")
                    self.assert_requires_new_proposal(task)
                    self.assertEqual(task.project_data()["project_id"], "test_project")
                    ready(task)
                    self.assertTrue(build_confirmed(task, actions(), lambda _: None))

    def test_restart_cannot_reuse_old_confirmation_after_manual_test_lock_removal(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                self.bound_ready(store)
            saved_task = store.path.read_bytes()
            changed = self.mutate_valid_context(store.directory)
            with store.exclusive(), self.assertRaises(ValueError):
                ConversationTask(store)
            self.assertTrue(store.lock_path.exists())
            self.assertEqual(store.path.read_bytes(), saved_task)
            # Only this isolated fixture's operator removes a known stopped lock.
            store.lock_path.unlink()
            reopened = TaskStore(Path(folder))
            with reopened.exclusive():
                recovered = recover_project_store(reopened, "test_project")
                self.assertEqual(recovered["state"], "clarifying")
                task = ConversationTask(reopened)
                self.assertEqual(task.project_data(), changed)
                self.assert_requires_new_proposal(task)
                ready(task)
                self.assertTrue(build_confirmed(task, actions(), lambda _: None))
            self.assertFalse(reopened.lock_path.exists())

    def test_wrong_project_identifier_does_not_adopt_or_modify_current_files(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.bound_ready(store)
                self.mutate_valid_context(store.directory)
                with self.assertRaises(ValueError):
                    task.project_data()
                before = {item.name: item.read_bytes() for item in store.directory.iterdir() if item.is_file()}
                with self.assertRaises(ValueError):
                    task.recover_project("other_project")
                after = {item.name: item.read_bytes() for item in store.directory.iterdir() if item.is_file()}
                self.assertEqual(after, before)
                self.assertTrue(store.project_uncertain)
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda _: self.fail("wrong project recovered"), lambda _: None)

    def test_missing_bound_context_is_not_replaced_with_empty_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = self.bound_ready(store)
                path = store.directory / CONTEXT_FILENAME
                path.unlink()
                with self.assertRaises(ValueError):
                    task.project_data()
                before = store.path.read_bytes()
                with self.assertRaises(ValueError):
                    task.recover_project("test_project")
                self.assertFalse(path.exists())
                self.assertEqual(store.path.read_bytes(), before)
                self.assertTrue(store.project_uncertain)

    def test_corrupt_context_or_revocation_is_rejected_without_repair(self):
        for corrupt in ("context", "revocation"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder))
                with store.exclusive():
                    task = self.bound_ready(store)
                    self.mutate_valid_context(store.directory)
                    with self.assertRaises(ValueError):
                        task.project_data()
                    name = CONTEXT_FILENAME if corrupt == "context" else project_session.REVOCATION_FILENAME
                    damaged = store.directory / name
                    damaged.write_bytes(b"corrupted fixture data")
                    before = {item.name: item.read_bytes() for item in store.directory.iterdir() if item.is_file()}
                    with self.assertRaises(ValueError):
                        task.recover_project("test_project")
                    after = {item.name: item.read_bytes() for item in store.directory.iterdir() if item.is_file()}
                    self.assertEqual(after, before)
                    self.assertTrue(store.project_uncertain)
                    self.assertTrue(store.retain_lock_on_close)

    def test_version_downgrade_is_not_accepted_as_first_initialization_recovery(self):
        for context_exists in (False, True):
            with self.subTest(context_exists=context_exists), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder))
                with store.exclusive():
                    self.bound_ready(store)
                    saved = json.loads(store.path.read_text(encoding="utf-8"))
                    saved["version"] = 1
                    saved.pop("context_binding")
                    saved.pop("confirmed_context_sha256")
                    store.path.write_text(json.dumps(saved), encoding="utf-8")
                    if not context_exists:
                        (store.directory / CONTEXT_FILENAME).unlink()
                    before = {item.name: item.read_bytes() for item in store.directory.iterdir() if item.is_file()}
                    with self.assertRaises(ValueError):
                        recover_project_store(store, "test_project")
                    after = {item.name: item.read_bytes() for item in store.directory.iterdir() if item.is_file()}
                    self.assertEqual(after, before)

    def test_running_completed_and_cancelled_orders_are_not_changed_by_recovery(self):
        for state in ("running", "completed", "cancelled"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder))
                with store.exclusive():
                    task = self.bound_ready(store)
                    if state == "completed":
                        self.assertTrue(build_confirmed(task, actions(), lambda _: None))
                    elif state == "cancelled":
                        task.cancel()
                    else:
                        task.change(state="running", event="Isolierter Test eines laufenden Auftrags.")
                    before = {item.name: item.read_bytes() for item in store.directory.iterdir() if item.is_file()}
                    with self.assertRaises(ValueError):
                        task.recover_project("test_project")
                    after = {item.name: item.read_bytes() for item in store.directory.iterdir() if item.is_file()}
                    self.assertEqual(after, before)
                    self.assertEqual(task.data["state"], state)

    def test_failed_recovery_write_retains_lock_and_cannot_restore_readiness(self):
        for after_task_write in (False, True):
            with self.subTest(after_task_write=after_task_write), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder))
                with store.exclusive():
                    task = self.bound_ready(store)
                    self.mutate_valid_context(store.directory)
                    with self.assertRaises(ValueError):
                        task.project_data()
                    save = store.save

                    def fail_save(data):
                        if after_task_write:
                            save(data)
                        raise OSError("injected recovery task write failure")

                    with patch.object(store, "save", side_effect=fail_save):
                        with self.assertRaises(OSError):
                            task.recover_project("test_project")
                    self.assertTrue(store.project_uncertain)
                    self.assertTrue(store.retain_lock_on_close)
                    self.assertTrue((store.directory / project_session.REVOCATION_FILENAME).exists())
                    with self.assertRaises(ValueError):
                        build_confirmed(task, lambda _: self.fail("failed recovery reached model"), lambda _: None)
                    task.change(event="Unabhängige Schreibung hebt Recovery-Sperre nicht auf.")
                    self.assertTrue(store.project_uncertain)
                    self.assertTrue(store.retain_lock_on_close)
                self.assertTrue(store.lock_path.exists())
                reopened = TaskStore(Path(folder))
                with self.assertRaises(RuntimeError):
                    with reopened.exclusive():
                        self.fail("another process acquired retained recovery lock")

    def test_cli_start_error_explains_explicit_recovery_and_recovery_start_revokes_old_approval(self):
        base = Path(__file__).resolve().parents[1] / "workspaces"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="recovery-cli-test-", dir=base) as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                self.bound_ready(store)
            original_task = store.path.read_bytes()
            context = store.directory / CONTEXT_FILENAME
            original_context = context.read_bytes()
            context.write_bytes(b"broken project context")
            blocked_model = Mock(side_effect=AssertionError("CLI made an unexpected model call"))
            output = io.StringIO()
            with patch.object(conversation_cli, "LMStudioChatClient", return_value=blocked_model), redirect_stdout(output):
                self.assertEqual(conversation_cli.main(["--workspace", str(store.directory)]), 1)
            blocked_model.assert_not_called()
            self.assertEqual(store.path.read_bytes(), original_task)
            self.assertEqual(context.read_bytes(), b"broken project context")
            self.assertTrue((store.directory / project_session.REVOCATION_FILENAME).is_file())
            self.assertTrue(store.lock_path.is_file())
            self.assertIn("Projektdateien", output.getvalue())
            self.assertIn("--recover-project-id", output.getvalue())
            self.assertIn("Prozessprüfung", output.getvalue())
            self.assertIn("manuellen Sperrfreigabe", output.getvalue())
            context.write_bytes(original_context)
            # Explicit stopped-process cleanup applies only to this test workspace.
            store.lock_path.unlink()
            terminal = conversation_cli.interact

            def eof(_):
                raise EOFError

            def terminal_with_eof(task, planner, builder):
                return terminal(task, planner, builder, read=eof)

            recovered_output = io.StringIO()
            with patch.object(conversation_cli, "LMStudioChatClient", return_value=blocked_model), \
                    patch.object(conversation_cli, "interact", side_effect=terminal_with_eof), \
                    redirect_stdout(recovered_output):
                self.assertEqual(conversation_cli.main(["--workspace", str(store.directory),
                                                       "--recover-project-id", "test_project"]), 0)
            blocked_model.assert_not_called()
            self.assertIn("Klärung nötig", recovered_output.getvalue())
            self.assertFalse(store.lock_path.exists())
            self.assertFalse((store.directory / project_session.REVOCATION_FILENAME).exists())
            self.assertEqual(context.read_bytes(), original_context)
            reopened = TaskStore(store.directory)
            with reopened.exclusive():
                task = ConversationTask(reopened)
                self.assert_requires_new_proposal(task)


if __name__ == "__main__":
    unittest.main()
