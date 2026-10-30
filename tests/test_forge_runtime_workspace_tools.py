import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.contracts import SubagentTask
from src.forge_runtime.workspace_tools import WorkspaceFileTools


class TestWorkspaceFileTools(unittest.TestCase):
    def make_tools(self, root, allowed_files=("src/example.py",)):
        task = SubagentTask(
            task_id="task-1",
            objective="Edit approved files",
            allowed_files=allowed_files,
        )
        return WorkspaceFileTools(root, task)

    def test_reads_allowed_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "src" / "example.py"
            path.parent.mkdir()
            path.write_text("original\n", encoding="utf-8")

            tools = self.make_tools(root)

            self.assertEqual(tools.read_text("src/example.py"), "original\n")

    def test_writes_allowed_file_and_creates_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.make_tools(root)

            tools.write_text("src/example.py", "created\n")

            self.assertEqual(
                (root / "src" / "example.py").read_text(encoding="utf-8"),
                "created\n",
            )
            self.assertEqual(list((root / "src").glob("*.tmp")), [])

    def test_overwrites_allowed_file_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "src" / "example.py"
            path.parent.mkdir()
            path.write_text("old\n", encoding="utf-8")
            tools = self.make_tools(root)

            tools.write_text("src/example.py", "new\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(list(path.parent.glob(".example.py.*.tmp")), [])

    def test_rejects_unapproved_file(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = self.make_tools(Path(directory))

            with self.assertRaisesRegex(ValueError, "not allowed"):
                tools.read_text("src/other.py")
            with self.assertRaisesRegex(ValueError, "not allowed"):
                tools.write_text("src/other.py", "content")

    def test_rejects_escape_and_absolute_paths_in_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ("../outside.py", str((root.parent / "outside.py").resolve()))
            for path in paths:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, "within the workspace"):
                        self.make_tools(root, (path,))

    def test_rejects_escape_and_absolute_paths_at_call_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = self.make_tools(root)
            paths = ("../outside.py", str((root.parent / "outside.py").resolve()))
            for path in paths:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, "within the workspace"):
                        tools.write_text(path, "content")

    def test_rejects_invalid_constructor_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("task-1", "Test", ("file.py",))

            with self.assertRaises(ValueError):
                WorkspaceFileTools(str(root), task)
            with self.assertRaises(ValueError):
                WorkspaceFileTools(root, object())
            with self.assertRaises(ValueError):
                WorkspaceFileTools(root / "missing", task)

    def test_rejects_subclass_task_and_content(self):
        class TaskSubclass(SubagentTask):
            pass

        class StringSubclass(str):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = TaskSubclass("task-1", "Test", ("file.py",))
            with self.assertRaises(ValueError):
                WorkspaceFileTools(root, task)

            tools = self.make_tools(root, ("file.py",))
            with self.assertRaises(ValueError):
                tools.write_text("file.py", StringSubclass("content"))

    def test_missing_allowed_file_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = self.make_tools(Path(directory))

            with self.assertRaises(FileNotFoundError):
                tools.read_text("src/example.py")


if __name__ == "__main__":
    unittest.main()
