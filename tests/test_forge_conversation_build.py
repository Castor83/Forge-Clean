import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_build import build_confirmed, check_completed, prepare_resume
from src.forge_runtime.conversation_cli import interact, main
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.contracts import PipelineResult, SubagentResult
from tests.test_forge_conversation_protocol import answer, proposal


def actions():
    values = [
        {"action": "write_file", "path": "agent/hello.py", "content": "def greeting(name):\n    return 'Hallo ' + name\n"},
        {"action": "finish", "passed": True, "summary": "Gebaut", "findings": []},
        {"action": "run_check"},
        {"action": "finish", "passed": True, "summary": "Geprüft", "findings": []},
        {"action": "run_check"},
        {"action": "finish", "passed": True, "summary": "Abgenommen", "findings": []}]
    iterator = iter(values)
    return lambda _: json.dumps(next(iterator))


def ready(task):
    discuss(task, "Hallo mit Name", lambda _: answer(proposal()))
    task.confirm()


class ConversationBuildTests(unittest.TestCase):
    def test_reopening_changed_completed_result_revokes_success(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                build_confirmed(task, actions(), lambda _: None)
            (Path(folder) / "build/agent/hello.py").write_text("def greeting(name):\n    return 'falsch'\n", encoding="utf-8")
            with store.exclusive():
                task = ConversationTask(store)
                check_completed(task)
                self.assertEqual(task.data["state"], "failed")
                self.assertFalse(task.data["report"]["host_passed"])
                self.assertFalse(task.data["report"].get("approved", False))

    def test_actual_pipeline_builds_and_independently_checks_function(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                output = []
                self.assertTrue(build_confirmed(task, actions(), output.append))
                self.assertEqual(task.data["state"], "completed")
                self.assertTrue(task.data["report"]["host_passed"])
                self.assertEqual(output[:2], ["Bauen: gestartet.", "Bauen: an Prüfung übergeben."])
                self.assertIn("Abnahme: bestanden.", output)
                self.assertEqual((Path(folder) / "build/agent/hello.py").read_text(), "def greeting(name):\n    return 'Hallo ' + name\n")
                with self.assertRaises(ValueError):
                    build_confirmed(task, actions())

    def test_no_build_without_confirmation_or_over_existing_user_file(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                discuss(task, "Hallo", lambda _: answer(proposal()))
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda _: self.fail("called"))
                task.confirm()
                path = Path(folder) / "build/agent/hello.py"
                path.parent.mkdir(parents=True)
                path.write_text("user file", encoding="utf-8")
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda _: self.fail("called"))
                self.assertEqual(path.read_text(), "user file")

    def test_false_pipeline_success_cannot_override_independent_host(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                false_success = PipelineResult(tuple(SubagentResult(role, True, "ok", ()) for role in ("builder", "tester", "reviewer")))
                with patch("src.forge_runtime.conversation_build.run_forge_task", return_value=false_success):
                    self.assertFalse(build_confirmed(task, actions(), lambda _: None))
                self.assertEqual(task.data["state"], "failed")
                self.assertFalse(task.data["report"]["host_passed"])

    def test_interrupt_reload_inspect_confirm_and_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                with patch("src.forge_runtime.conversation_build.run_forge_task", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        build_confirmed(task, actions(), lambda _: None)
                self.assertEqual(task.data["state"], "interrupted")
            with store.exclusive():
                task = ConversationTask(store)
                with self.assertRaises(ValueError):
                    task.confirm()
                files, passed, detail = prepare_resume(task)
                self.assertEqual(files, {})
                self.assertFalse(passed)
                task.confirm()
                self.assertTrue(build_confirmed(task, actions(), lambda _: None))

    def test_resume_rejects_files_changed_after_inspection(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                with patch("src.forge_runtime.conversation_build.run_forge_task", side_effect=RuntimeError("model offline")):
                    with self.assertRaises(RuntimeError):
                        build_confirmed(task, actions(), lambda _: None)
                self.assertEqual(task.data["state"], "failed")
                prepare_resume(task)
                task.confirm()
                path = Path(folder) / "build/agent/hello.py"
                path.parent.mkdir()
                path.write_text("changed", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "verändert"):
                    build_confirmed(task, actions())

    def test_terminal_full_flow_and_cancel(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                inputs = iter(["Hallo mit Name", "/bestaetigen", "/bauen", "/status", "/ende"])
                output = []
                self.assertEqual(interact(task, lambda _: answer(proposal()), actions(), lambda _: next(inputs), output.append), 0)
                self.assertEqual(task.data["state"], "completed")
                self.assertTrue(any("Ada" in line and "Hallo Ada" in line for line in output))
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                interact(task, None, None, lambda _: "/abbrechen", lambda _: None)
                self.assertEqual(task.data["state"], "cancelled")

    def test_cli_rejects_workspace_outside_repository_and_dot_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(main(["--workspace", folder]), 1)
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(main(["--workspace", str(root / "workspaces/../tests")]), 1)
