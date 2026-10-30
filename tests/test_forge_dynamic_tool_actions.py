import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.forge_runtime.contracts import SubagentTask
from src.forge_runtime.tool_loop import run_tool_loop
from src.forge_runtime.workspace_tools import WorkspaceFileTools


class RecordingChat:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.current = ()
        self.schemas_seen = []

    def set_tool_actions(self, actions):
        self.current = actions

    def __call__(self, messages):
        self.schemas_seen.append(self.current)
        return json.dumps(next(self.actions))


FINISH = {"action": "finish", "passed": True, "summary": "done", "findings": []}


class TestDynamicToolActions(unittest.TestCase):
    def test_created_file_switches_to_replacement_before_next_model_call(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("chunked", "build in chunks", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = RecordingChat([
                {"action": "write_file", "path": "agent.py", "content": "first\n# NEXT\n"},
                {"action": "replace_text", "path": "agent.py", "old": "# NEXT", "new": "second"},
                FINISH,
            ])
            result = run_tool_loop("builder", task, (), workspace, chat)
            self.assertTrue(result.passed)
            self.assertIn("write_file", chat.schemas_seen[0])
            self.assertNotIn("write_file", chat.schemas_seen[1])
            self.assertIn("replace_text", chat.schemas_seen[1])
            self.assertEqual((root / "agent.py").read_text(), "first\nsecond\n")

    def test_creation_stays_available_until_all_writable_files_exist(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("multiple", "build two files", ("a.py", "b.py"))
            chat = RecordingChat([
                {"action": "write_file", "path": "a.py", "content": "a"},
                {"action": "write_file", "path": "b.py", "content": "b"},
                FINISH,
            ])
            result = run_tool_loop("builder", task, (), WorkspaceFileTools(root, task), chat)
            self.assertTrue(result.passed)
            self.assertIn("write_file", chat.schemas_seen[1])
            self.assertNotIn("write_file", chat.schemas_seen[2])
            self.assertEqual((root / "a.py").read_text(), "a")
            self.assertEqual((root / "b.py").read_text(), "b")

    def test_missing_read_only_input_does_not_keep_creation_available(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("readonly", "build one file", ("agent.py",), ("SPEC.md",))
            chat = RecordingChat([
                {"action": "write_file", "path": "agent.py", "content": "generated"},
                FINISH,
            ])
            run_tool_loop("builder", task, (), WorkspaceFileTools(root, task), chat)
            self.assertNotIn("write_file", chat.schemas_seen[1])
            self.assertFalse((root / "SPEC.md").exists())


if __name__ == "__main__":
    unittest.main()
