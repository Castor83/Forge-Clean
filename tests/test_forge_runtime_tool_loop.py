import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.forge_runtime.contracts import SubagentResult, SubagentTask
from src.forge_runtime.tool_loop import (
    FinishAction,
    ReadFileAction,
    ToolLoopDeadlineExceeded,
    WriteFileAction,
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


class TestToolActions(unittest.TestCase):
    def test_parses_all_actions(self):
        self.assertEqual(
            parse_tool_action('{"action":"read_file","path":"src/example.py"}'),
            ReadFileAction("src/example.py"),
        )
        self.assertEqual(
            parse_tool_action(
                '{"action":"write_file","path":"src/example.py",'
                '"content":"value = 1\\n"}'
            ),
            WriteFileAction("src/example.py", "value = 1\n"),
        )
        self.assertEqual(
            parse_tool_action(
                '{"action":"finish","passed":true,"summary":"Done",'
                '"findings":["Created file"]}'
            ),
            FinishAction(True, "Done", ("Created file",)),
        )

    def test_rejects_malformed_unknown_and_extra_fields(self):
        responses = (
            "not json",
            "[]",
            "{}",
            '{"action":"unknown"}',
            '{"action":"read_file","path":"file.py","extra":1}',
            '{"action":"write_file","path":"file.py"}',
            '{"action":"finish","passed":true,"summary":"Done",'
            '"findings":[],"extra":1}',
        )
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    parse_tool_action(response)

    def test_rejects_invalid_action_field_types(self):
        responses = (
            '{"action":"read_file","path":""}',
            '{"action":"write_file","path":"file.py","content":1}',
            '{"action":"finish","passed":1,"summary":"Done","findings":[]}',
            '{"action":"finish","passed":true,"summary":" ","findings":[]}',
            '{"action":"finish","passed":true,"summary":"Done","findings":[1]}',
        )
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    parse_tool_action(response)


class TestRunToolLoop(unittest.TestCase):
    def make_workspace(self, root, role="builder"):
        task = SubagentTask(
            task_id="task-1",
            objective="Build example",
            allowed_files=("src/example.py",),
        )
        return task, WorkspaceFileTools(root, task)

    def test_builder_reads_replaces_and_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "src" / "example.py"
            path.parent.mkdir()
            path.write_text("old\n", encoding="utf-8")
            task, workspace = self.make_workspace(root)
            chat = ScriptedChat(
                (
                    '{"action":"read_file","path":"src/example.py"}',
                    '{"action":"replace_text","path":"src/example.py",'
                    '"old":"old\\n","new":"new\\n"}',
                    '{"action":"finish","passed":true,"summary":"Built",'
                    '"findings":["Updated example"]}',
                )
            )

            result = run_tool_loop("builder", task, (), workspace, chat)

            self.assertEqual(
                result,
                SubagentResult("builder", True, "Built", ("Updated example",)),
            )
            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(len(chat.calls), 3)
            self.assertEqual(
                chat.calls[1][-1],
                (
                    "user",
                    '{"content":"old\\n","kind":"untrusted_file_data",'
                    '"path":"src/example.py","source":"workspace_file",'
                    '"trusted":false}\n',
                ),
            )
            self.assertEqual(
                chat.calls[2][-1],
                ("user", '{"path":"src/example.py","status":"replaced"}\n'),
            )

    def test_tester_may_read_but_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "src" / "example.py"
            path.parent.mkdir()
            path.write_text("content\n", encoding="utf-8")
            task, workspace = self.make_workspace(root)
            builder = SubagentResult("builder", True, "Built", ())
            chat = ScriptedChat(
                ('{"action":"write_file","path":"src/example.py","content":"bad"}',)
            )

            with self.assertRaisesRegex(ValueError, "only builder may write"):
                run_tool_loop("tester", task, (builder,), workspace, chat)
            self.assertEqual(path.read_text(encoding="utf-8"), "content\n")

    def test_missing_and_large_reads_return_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, workspace = self.make_workspace(root)
            chat = ScriptedChat(
                (
                    '{"action":"read_file","path":"src/example.py"}',
                    '{"action":"finish","passed":false,"summary":"Missing",'
                    '"findings":[]}',
                )
            )
            run_tool_loop("builder", task, (), workspace, chat)
            self.assertEqual(
                chat.calls[1][-1],
                ("user", '{"error":"file_not_found","path":"src/example.py"}\n'),
            )

            path = root / "src" / "example.py"
            path.parent.mkdir(exist_ok=True)
            path.write_text("12345", encoding="utf-8")
            chat = ScriptedChat(
                (
                    '{"action":"read_file","path":"src/example.py"}',
                    '{"action":"finish","passed":false,"summary":"Large",'
                    '"findings":[]}',
                )
            )
            run_tool_loop("builder", task, (), workspace, chat, max_file_chars=4)
            self.assertEqual(
                chat.calls[1][-1],
                ("user", '{"error":"file_too_large","path":"src/example.py"}\n'),
            )

    def test_rejects_large_write_before_file_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, workspace = self.make_workspace(root)
            chat = ScriptedChat(
                ('{"action":"write_file","path":"src/example.py","content":"12345"}',)
            )

            with self.assertRaisesRegex(ValueError, "exceeds max_file_chars"):
                run_tool_loop(
                    "builder", task, (), workspace, chat, max_file_chars=4
                )
            self.assertFalse((root / "src" / "example.py").exists())

    def test_stops_after_maximum_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, workspace = self.make_workspace(root)
            chat = ScriptedChat(
                (
                    '{"action":"read_file","path":"src/example.py"}',
                    '{"action":"read_file","path":"src/example.py"}',
                )
            )

            with self.assertRaisesRegex(RuntimeError, "exceeded max_steps"):
                run_tool_loop("builder", task, (), workspace, chat, max_steps=2)
            self.assertEqual(len(chat.calls), 2)

    def test_model_call_deadline_stops_before_delayed_action_is_accepted(self):
        release = threading.Event()

        class DelayedChat:
            def __call__(self, messages):
                release.wait(1)
                return (
                    '{"action":"write_file","path":"src/example.py",'
                    '"content":"too late"}'
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, workspace = self.make_workspace(root)
            started = time.monotonic()
            try:
                with self.assertRaises(ToolLoopDeadlineExceeded) as caught:
                    run_tool_loop(
                        "builder",
                        task,
                        (),
                        workspace,
                        DelayedChat(),
                        max_call_seconds=0.02,
                        max_elapsed_seconds=1,
                    )
            finally:
                release.set()

            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(caught.exception.kind, "call")
            self.assertEqual(caught.exception.role, "builder")
            self.assertEqual(caught.exception.step, 1)
            self.assertEqual(caught.exception.last_action, "none")
            self.assertIn("model call deadline exceeded", str(caught.exception))
            self.assertFalse((root / "src" / "example.py").exists())

    def test_role_deadline_reports_last_completed_action(self):
        release = threading.Event()

        class ReadThenWaitChat:
            def __init__(self):
                self.calls = 0

            def __call__(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return '{"action":"read_file","path":"src/example.py"}'
                release.wait(1)
                return (
                    '{"action":"finish","passed":true,'
                    '"summary":"late","findings":[]}'
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, workspace = self.make_workspace(root)
            try:
                with self.assertRaises(ToolLoopDeadlineExceeded) as caught:
                    run_tool_loop(
                        "builder",
                        task,
                        (),
                        workspace,
                        ReadThenWaitChat(),
                        max_call_seconds=1,
                        max_elapsed_seconds=0.02,
                    )
            finally:
                release.set()

            self.assertEqual(caught.exception.kind, "role")
            self.assertEqual(caught.exception.step, 2)
            self.assertEqual(caught.exception.last_action, "read_file")
            self.assertIn("role deadline exceeded", str(caught.exception))

    def test_rejects_invalid_loop_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            task, workspace = self.make_workspace(Path(directory))
            valid_chat = ScriptedChat(
                ('{"action":"finish","passed":true,"summary":"Done","findings":[]}',)
            )
            invalid_calls = (
                {"workspace": object(), "chat": valid_chat},
                {"workspace": workspace, "chat": None},
                {"workspace": workspace, "chat": valid_chat, "max_steps": True},
                {"workspace": workspace, "chat": valid_chat, "max_file_chars": 0},
                {"workspace": workspace, "chat": valid_chat, "max_call_seconds": True},
                {"workspace": workspace, "chat": valid_chat, "max_call_seconds": float("inf")},
                {"workspace": workspace, "chat": valid_chat, "max_elapsed_seconds": 0},
                {"workspace": workspace, "chat": valid_chat, "max_elapsed_seconds": float("nan")},
            )
            for arguments in invalid_calls:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        run_tool_loop("builder", task, (), **arguments)


if __name__ == "__main__":
    unittest.main()
