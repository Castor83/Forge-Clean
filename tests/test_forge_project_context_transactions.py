import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime import project_session
from src.forge_runtime.conversation_build import build_confirmed, check_completed
from src.forge_runtime.conversation_cli import interact
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.project_context import CONTEXT_FILENAME, fresh_project_context
from tests.test_forge_conversation_build import actions, ready
from tests.test_forge_conversation_protocol import answer, proposal


class ProjectContextTransactionTests(unittest.TestCase):
    def test_initialization_errors_before_and_after_each_file_replace_retain_lock(self):
        for failed_write in (1, 2, 3):
            for after in (False, True):
                with self.subTest(write=failed_write, after=after), tempfile.TemporaryDirectory() as folder:
                    store = TaskStore(Path(folder))
                    with store.exclusive():
                        task = ConversationTask(store)
                        ready(task)
                        original = project_session._atomic_json
                        writes = []

                        def write(path, data, maximum):
                            writes.append(path.name)
                            if len(writes) == failed_write and not after:
                                raise OSError("injected before replacement")
                            original(path, data, maximum)
                            if len(writes) == failed_write and after:
                                raise OSError("injected after replacement")

                        with patch.object(project_session, "_atomic_json", side_effect=write):
                            with self.assertRaises(OSError):
                                task.initialize_project("test_project")
                        self.assertTrue(store.project_uncertain)
                        self.assertTrue(store.retain_lock_on_close)
                        task.change(event="Unabhängige Statusschreibung löst keine Projekttransaktion.")
                        self.assertTrue(store.retain_lock_on_close)
                        with self.assertRaises(ValueError):
                            build_confirmed(task, lambda _: self.fail("model called"), lambda _: None)
                        with self.assertRaises(ValueError):
                            task.cancel()
                    self.assertTrue(store.lock_path.exists())
                    with self.assertRaises(RuntimeError):
                        with TaskStore(Path(folder)).exclusive():
                            self.fail("second process opened uncertain task")

    def test_task_write_errors_and_interrupts_also_retain_transaction_lock(self):
        for after in (False, True):
            for error_type in (OSError, KeyboardInterrupt):
                with self.subTest(after=after, error=error_type), tempfile.TemporaryDirectory() as folder:
                    store = TaskStore(Path(folder))
                    with store.exclusive():
                        task = ConversationTask(store)
                        ready(task)
                        save = store.save

                        def fail(data):
                            if after:
                                save(data)
                            raise error_type("injected task save failure")

                        with patch.object(store, "save", side_effect=fail):
                            with self.assertRaises(error_type):
                                task.initialize_project("test_project")
                        self.assertTrue(store.project_uncertain)
                        task.change(event="Spätere Schreibung ist keine Recovery.")
                        self.assertTrue(store.retain_lock_on_close)
                    self.assertTrue(store.lock_path.exists())

    def test_constructor_failure_preserves_file_and_durably_revokes_old_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            store = TaskStore(directory)
            with store.exclusive():
                task = ConversationTask(store)
                task.initialize_project("test_project")
                ready(task)
            task_bytes = store.path.read_bytes()
            context = directory / CONTEXT_FILENAME
            original_context = context.read_bytes()
            context.write_bytes(b"broken")
            with store.exclusive():
                with self.assertRaises(ValueError):
                    ConversationTask(store)
            self.assertEqual(store.path.read_bytes(), task_bytes)
            self.assertEqual(context.read_bytes(), b"broken")
            self.assertTrue((directory / project_session.REVOCATION_FILENAME).exists())
            context.write_bytes(original_context)
            # Simulate explicit operator lock removal in this isolated fixture.
            store.lock_path.unlink()
            reopened = TaskStore(directory)
            with reopened.exclusive():
                with self.assertRaisesRegex(ValueError, "widerrufen"):
                    ConversationTask(reopened)
            self.assertEqual(store.path.read_bytes(), task_bytes)

    def test_legacy_context_appearing_during_first_reply_blocks_the_next_action(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                calls = []

                def model(messages):
                    calls.append(messages)
                    (Path(folder) / CONTEXT_FILENAME).write_text(
                        json.dumps(fresh_project_context("test_project")), encoding="utf-8")
                    return json.dumps({"action": "write_file", "path": "agent/hello.py",
                                       "content": "def greeting(name):\n    return 'Hallo ' + name\n"})

                with self.assertRaises(ValueError):
                    build_confirmed(task, model, lambda _: None)
                self.assertEqual(len(calls), 1)
                self.assertFalse((Path(folder) / "build/agent/hello.py").exists())
                self.assertEqual(task.data["state"], "failed")

    def test_context_change_in_final_host_check_prevents_completed_state(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                task.initialize_project("test_project")
                ready(task)
                from src.forge_runtime import conversation_build
                verify = conversation_build.verify_forge_task

                def changed_after_check(root, spec):
                    result = verify(root, spec)
                    path = Path(folder) / CONTEXT_FILENAME
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["next_step"] = "Geänderter Schritt während Hostprüfung."
                    path.write_text(json.dumps(data), encoding="utf-8")
                    return result

                with patch.object(conversation_build, "verify_forge_task", side_effect=changed_after_check):
                    with self.assertRaises(ValueError):
                        build_confirmed(task, actions(), lambda _: None)
                self.assertEqual(task.data["state"], "failed")
                self.assertFalse(task.data["report"].get("approved", False))

    def test_real_terminal_project_commands_persist_and_build_with_current_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                inputs = iter(["/projekt_anlegen test_project", "/regel stil Einfach erklären.",
                    "/regel stil Kurz und verständlich erklären.",
                    "/entscheidung ziel Kleine Begrüßung bauen.",
                    "/aufgabe greeting offen Begrüßung prüfen.", "/naechster Begrüßung bauen.",
                    "Hallo mit Name", "/bestaetigen", "/bauen", "/projekt", "/ende"])
                calls = []

                def planner(messages):
                    calls.append(messages)
                    return answer(proposal())

                output = []
                self.assertEqual(interact(task, planner, actions(), lambda _: next(inputs), output.append), 0)
                self.assertEqual(task.data["state"], "completed")
                self.assertTrue(task.data["report"]["approved"])
                self.assertTrue(any("Kurz und verständlich" in line for line in output))
                envelope = json.loads(calls[0][1][1])
                self.assertEqual([rule["text"] for rule in envelope["context"]["rules"]],
                                 ["Kurz und verständlich erklären."])
                self.assertEqual(envelope["context"]["next_step"], "Begrüßung bauen.")
                self.assertEqual(len(task.project_data()["rules"]), 2)

    def test_invalid_rule_change_revokes_confirmation_without_changing_context(self):
        for text in ("", "x" * 2001):
            with self.subTest(text_length=len(text)), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder))
                with store.exclusive():
                    task = ConversationTask(store)
                    task.initialize_project("test_project")
                    ready(task)
                    before = (Path(folder) / CONTEXT_FILENAME).read_bytes()
                    with self.assertRaises(ValueError):
                        project_session.update_project(task, "rules", "style", text)
                    self.assertEqual((Path(folder) / CONTEXT_FILENAME).read_bytes(), before)
                    self.assertFalse(task.is_confirmed())
                    self.assertIsNone(task.data["confirmed_context_sha256"])
                    with self.assertRaises(ValueError):
                        build_confirmed(task, lambda _: self.fail("model called"), lambda _: None)
                with TaskStore(Path(folder)).exclusive() as reopened:
                    self.assertFalse(ConversationTask(reopened).is_confirmed())

    def test_change_during_success_save_emits_no_success_and_retains_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                task.initialize_project("test_project")
                ready(task)
                save = store.save

                def mutate_on_success(data):
                    save(data)
                    if data["state"] == "completed":
                        path = Path(folder) / CONTEXT_FILENAME
                        changed = json.loads(path.read_text(encoding="utf-8"))
                        changed["next_step"] = "Geänderter Stand nach Ergebnisspeicherung."
                        path.write_text(json.dumps(changed), encoding="utf-8")

                output = []
                with patch.object(store, "save", side_effect=mutate_on_success):
                    with self.assertRaises(ValueError):
                        build_confirmed(task, actions(), output.append)
                self.assertFalse(any(line.startswith("Fertig:") for line in output))
                self.assertEqual(task.data["state"], "failed")
                self.assertTrue(store.retain_lock_on_close)
            self.assertTrue(store.lock_path.exists())

    def test_completed_file_recheck_also_checks_context_after_host_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                task.initialize_project("test_project")
                ready(task)
                self.assertTrue(build_confirmed(task, actions(), lambda _: None))
                from src.forge_runtime import conversation_build
                verify = conversation_build.verify_forge_task

                def mutate_on_check(root, spec):
                    result = verify(root, spec)
                    path = Path(folder) / CONTEXT_FILENAME
                    changed = json.loads(path.read_text(encoding="utf-8"))
                    changed["next_step"] = "Geändert während Abschluss-Nachprüfung."
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    return result

                with patch.object(conversation_build, "verify_forge_task", side_effect=mutate_on_check):
                    with self.assertRaises(ValueError):
                        check_completed(task)
                self.assertTrue(store.project_uncertain)
                self.assertTrue((Path(folder) / project_session.REVOCATION_FILENAME).exists())
