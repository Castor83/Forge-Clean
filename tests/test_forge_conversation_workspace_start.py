import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime import conversation_cli as cli


class ConversationWorkspaceStartTests(unittest.TestCase):
    def start_in(self, root, workspace):
        fake_source = root / "src/forge_runtime/conversation_cli.py"
        with patch.object(cli, "__file__", str(fake_source)), patch.object(cli, "LMStudioChatClient", side_effect=lambda *args, **kwargs: lambda _: self.fail("Model called")), patch.object(cli, "interact", return_value=0), patch("builtins.print"):
            return cli.main(["--workspace", str(workspace)])

    def test_parent_escape_is_rejected_before_creating_target_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "workspaces"
            base.mkdir()
            target = base / ".." / "unapproved" / "child"
            self.assertEqual(self.start_in(root, target), 1)
            self.assertFalse((root / "unapproved").exists())
            self.assertEqual(list(base.iterdir()), [])

    def test_alias_of_shared_base_is_rejected_before_creating_intermediate_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "workspaces"
            base.mkdir()
            self.assertEqual(self.start_in(root, base / "unused" / ".."), 1)
            self.assertEqual(list(base.iterdir()), [])

    def test_valid_new_nested_task_gets_exclusive_session_and_no_lingering_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "workspaces"
            base.mkdir()
            directory = base / "group" / "task"
            self.assertEqual(self.start_in(root, directory), 0)
            self.assertTrue(directory.is_dir())
            self.assertFalse((directory / "conversation.lock").exists())
            self.assertEqual(list(directory.iterdir()), [])
