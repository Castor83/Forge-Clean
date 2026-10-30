import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.contracts import SubagentResult, SubagentTask
from src.forge_runtime.pipeline import run_review_pipeline
from src.forge_runtime.tool_executor import ForgeToolExecutor
from src.forge_runtime.tool_loop import (
    RunCheckAction,
    parse_tool_action,
    run_tool_loop,
)
from src.forge_runtime.workspace_tools import WorkspaceFileTools


class ScriptedChat:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        return self.responses.pop(0)


class TestHostCheck(unittest.TestCase):
    def test_parses_only_exact_run_check_action(self):
        self.assertEqual(parse_tool_action('{"action":"run_check"}'), RunCheckAction())
        with self.assertRaises(ValueError):
            parse_tool_action('{"action":"run_check","command":"whoami"}')

    def test_pipeline_requires_fixed_successful_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("safe-check", "Build safely", ("agent.py",))
            chat = ScriptedChat(
                (
                    '{"action":"write_file","path":"agent.py","content":"x = 1\\n"}',
                    '{"action":"finish","passed":true,"summary":"Built","findings":[]}',
                    '{"action":"read_file","path":"agent.py"}',
                    '{"action":"run_check"}',
                    '{"action":"finish","passed":true,"summary":"Tested","findings":[]}',
                    '{"action":"read_file","path":"agent.py"}',
                    '{"action":"run_check"}',
                    '{"action":"finish","passed":true,"summary":"Reviewed","findings":[]}',
                )
            )
            calls = []

            def check_runner():
                calls.append(True)
                return True, "1 fixed check passed"

            result = run_review_pipeline(
                task,
                ForgeToolExecutor(root, chat, check_runner=check_runner),
            )

            self.assertTrue(result.approved)
            self.assertEqual(len(calls), 2)
            self.assertNotIn("run_check", chat.calls[0][0][1])
            self.assertIn("run_check", chat.calls[2][0][1])
            self.assertIn("run_check", chat.calls[5][0][1])
            self.assertEqual(
                chat.calls[4][-1],
                (
                    "user",
                    '{"output":"1 fixed check passed","passed":true,'
                    '"status":"check_complete"}\n',
                ),
            )

    def test_missing_check_gets_one_bounded_chance_to_run_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("safe-check", "Test safely", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            builder = SubagentResult("builder", True, "Built", ())
            chat = ScriptedChat(
                (
                    '{"action":"finish","passed":true,"summary":"Skipped","findings":[]}',
                    '{"action":"run_check"}',
                    '{"action":"finish","passed":true,"summary":"Checked","findings":[]}',
                )
            )

            result = run_tool_loop(
                "tester",
                task,
                (builder,),
                workspace,
                chat,
                check_runner=lambda: (True, "ok"),
            )

            self.assertTrue(result.passed)
            self.assertEqual(
                chat.calls[1][-1],
                (
                    "user",
                    '{"error":"host_check_required","required_action":"run_check"}\n',
                ),
            )

    def test_positive_result_remains_blocked_when_check_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("safe-check", "Test safely", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            builder = SubagentResult("builder", True, "Built", ())
            responses = (
                '{"action":"run_check"}',
                '{"action":"finish","passed":true,"summary":"Ignored","findings":[]}',
            )

            with self.assertRaisesRegex(ValueError, "successful host check"):
                run_tool_loop(
                    "tester",
                    task,
                    (builder,),
                    workspace,
                    ScriptedChat(responses),
                    check_runner=lambda: (False, "failed"),
                )

    def test_missing_check_cannot_escape_step_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("safe-check", "Test safely", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            builder = SubagentResult("builder", True, "Built", ())

            with self.assertRaisesRegex(RuntimeError, "exceeded max_steps"):
                run_tool_loop(
                    "tester",
                    task,
                    (builder,),
                    workspace,
                    ScriptedChat(
                        (
                            '{"action":"finish","passed":true,"summary":"Skipped","findings":[]}',
                        )
                    ),
                    max_steps=1,
                    check_runner=lambda: (True, "ok"),
                )

    def test_rejects_unsafe_or_invalid_check_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("safe-check", "Build safely", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            builder = SubagentResult("builder", True, "Built", ())

            with self.assertRaisesRegex(ValueError, "builder may not run"):
                run_tool_loop(
                    "builder",
                    task,
                    (),
                    workspace,
                    ScriptedChat(('{"action":"run_check"}',)),
                    check_runner=lambda: (True, "ok"),
                )
            with self.assertRaisesRegex(ValueError, r"tuple\[bool, str\]"):
                run_tool_loop(
                    "tester",
                    task,
                    (builder,),
                    workspace,
                    ScriptedChat(('{"action":"run_check"}',)),
                    check_runner=lambda: [True, "ok"],
                )
            with self.assertRaisesRegex(ValueError, "callable or None"):
                ForgeToolExecutor(root, ScriptedChat(()), check_runner=True)


if __name__ == "__main__":
    unittest.main()
