import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_build import build_confirmed, check_completed
from src.forge_runtime.conversation_cli import interact
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.contracts import SubagentTask
from src.forge_runtime.task_runner import (
    ForgeTaskSpec, FunctionCase, StaticCheck, _run_restricted_function_cases, verify_forge_task)
from tests.test_forge_conversation_build import actions, ready


def deep_source(count, name="check"):
    return f"def {name}(value):\n    return " + "value + " * count + '"x"\n'


class RestrictedExpressionDepthTests(unittest.TestCase):
    def test_actual_parser_depth_limit_is_a_negative_fixed_check_for_both_kinds(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / "agent/check.py"
            path.parent.mkdir()
            source = deep_source(3500)
            path.write_text(source, encoding="utf-8")
            checks = (
                StaticCheck("python_syntax", "agent/check.py"),
                StaticCheck("python_function_cases", "agent/check.py", function="check",
                    cases=(FunctionCase(("",), "x"),)))
            for check in checks:
                spec = ForgeTaskSpec(SubagentTask("depth-check", "Prüfe die Datei.", ("agent/check.py",)), (check,))
                with self.subTest(kind=check.kind):
                    passed, detail = verify_forge_task(directory, spec)
                    self.assertFalse(passed)
                    self.assertIn("complexity limit", detail)
            self.assertEqual(path.read_text(encoding="utf-8"), source)

    def test_expression_within_current_depth_limit_still_passes(self):
        check = StaticCheck("python_function_cases", "agent/check.py", function="check",
            cases=(FunctionCase(("",), "x"),))
        self.assertIsNone(_run_restricted_function_cases(deep_source(1200), check))

    def test_reopening_deep_changed_completed_code_keeps_the_conversation_available(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                self.assertTrue(build_confirmed(task, actions(), lambda _: None))
            path = Path(folder) / "build/agent/hello.py"
            source = deep_source(3500, "greeting")
            path.write_text(source, encoding="utf-8")
            with store.exclusive():
                task = ConversationTask(store)
                check_completed(task)
                self.assertEqual(task.data["state"], "failed")
                self.assertFalse(task.data["report"]["host_passed"])
                self.assertIn("complexity limit", task.data["report"]["host_detail"])
                output = []
                self.assertEqual(interact(task, None, None, lambda _: "/ende", output.append), 0)
                self.assertTrue(any("Fehlgeschlagen" in line for line in output))
            self.assertEqual(path.read_text(encoding="utf-8"), source)
