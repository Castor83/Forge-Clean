import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.contracts import SubagentTask
from src.forge_runtime.pipeline import run_review_pipeline
from src.forge_runtime.tool_executor import ForgeToolExecutor


class ScriptedChat:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        return self.responses.pop(0)


class TestForgeToolExecutor(unittest.TestCase):
    def test_complete_builder_tester_reviewer_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask(
                task_id="harmless-agent",
                objective="Create and independently review a harmless example",
                allowed_files=("src/example.py",),
            )
            chat = ScriptedChat(
                (
                    '{"action":"write_file","path":"src/example.py",'
                    '"content":"def answer():\\n    return 42\\n"}',
                    '{"action":"finish","passed":true,"summary":"Built",'
                    '"findings":["Created example"]}',
                    '{"action":"read_file","path":"src/example.py"}',
                    '{"action":"finish","passed":true,"summary":"Tested",'
                    '"findings":["Content is correct"]}',
                    '{"action":"read_file","path":"src/example.py"}',
                    '{"action":"finish","passed":true,"summary":"Reviewed",'
                    '"findings":["Approved"]}',
                )
            )
            executor = ForgeToolExecutor(root, chat)

            result = run_review_pipeline(task, executor)

            self.assertTrue(result.approved)
            self.assertEqual(
                tuple(item.role for item in result.results),
                ("builder", "tester", "reviewer"),
            )
            self.assertEqual(
                (root / "src" / "example.py").read_text(encoding="utf-8"),
                "def answer():\n    return 42\n",
            )
            self.assertEqual(len(chat.calls), 6)
            self.assertIn("write_file", chat.calls[0][0][1])
            self.assertIn("may not write files", chat.calls[2][0][1])
            self.assertIn("may not write files", chat.calls[4][0][1])

    def test_builder_failure_stops_later_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("task-1", "Build", ("file.py",))
            chat = ScriptedChat(
                (
                    '{"action":"finish","passed":false,'
                    '"summary":"Could not build","findings":["No change"]}',
                )
            )

            result = run_review_pipeline(task, ForgeToolExecutor(root, chat))

            self.assertFalse(result.approved)
            self.assertEqual(len(result.results), 1)
            self.assertEqual(result.results[0].role, "builder")
            self.assertEqual(len(chat.calls), 1)

    def test_workspace_rejects_unapproved_model_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("task-1", "Build", ("allowed.py",))
            chat = ScriptedChat(
                ('{"action":"write_file","path":"other.py","content":"bad"}',)
            )

            with self.assertRaisesRegex(ValueError, "not allowed"):
                ForgeToolExecutor(root, chat)("builder", task, ())
            self.assertFalse((root / "other.py").exists())

    def test_rejects_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chat = ScriptedChat(())
            invalid_arguments = (
                {"root": str(root), "chat": chat},
                {"root": root / "missing", "chat": chat},
                {"root": root, "chat": None},
                {"root": root, "chat": chat, "max_steps": True},
                {"root": root, "chat": chat, "max_file_chars": 0},
                {"root": root, "chat": chat, "max_call_seconds": True},
                {"root": root, "chat": chat, "max_call_seconds": float("inf")},
                {"root": root, "chat": chat, "max_elapsed_seconds": 0},
                {"root": root, "chat": chat, "max_elapsed_seconds": float("nan")},
            )
            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        ForgeToolExecutor(**arguments)


if __name__ == "__main__":
    unittest.main()
