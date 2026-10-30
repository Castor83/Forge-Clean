import tempfile
import unittest
from pathlib import Path

from src.agent1_workspace.health import is_workspace_ready, missing_required_files


class TestHealth(unittest.TestCase):
    def test_all_files_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            
            # Create all required files
            (root / "AGENTS.md").touch()
            (root / "PAUSE_STATE.md").touch()
            (root / "scripts").mkdir()
            (root / "scripts" / "verify.ps1").touch()
            
            self.assertEqual(missing_required_files(root), [])
            self.assertTrue(is_workspace_ready(root))

    def test_missing_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            
            # Create only one required file
            (root / "PAUSE_STATE.md").touch()
            
            # Should return the missing files in alphabetical order
            missing = missing_required_files(root)
            self.assertEqual(missing, ["AGENTS.md", "scripts/verify.ps1"])
            self.assertFalse(is_workspace_ready(root))

    def test_missing_all_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            
            # Create a directory named AGENTS.md instead of a file
            (root / "AGENTS.md").mkdir()
            
            # Should return all files in alphabetical order
            missing = missing_required_files(root)
            self.assertEqual(missing, ["AGENTS.md", "PAUSE_STATE.md", "scripts/verify.ps1"])
            self.assertFalse(is_workspace_ready(root))

    def test_missing_one_file_specific(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            
            # Create AGENTS.md as directory (as requested in the task)
            (root / "AGENTS.md").mkdir()
            
            # Create PAUSE_STATE.md and scripts/verify.ps1 as files
            (root / "PAUSE_STATE.md").touch()
            (root / "scripts").mkdir()
            (root / "scripts" / "verify.ps1").touch()
            
            # Should return only AGENTS.md (as it's a directory, not a file)
            missing = missing_required_files(root)
            self.assertEqual(missing, ["AGENTS.md"])
            self.assertFalse(is_workspace_ready(root))

    def test_missing_one_file_as_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            
            # Create AGENTS.md as directory (as requested in the task)
            (root / "AGENTS.md").mkdir()
            
            # Create PAUSE_STATE.md and scripts/verify.ps1 as files
            (root / "PAUSE_STATE.md").touch()
            (root / "scripts").mkdir()
            (root / "scripts" / "verify.ps1").touch()
            
            # Should directly return ['AGENTS.md'] 
            self.assertEqual(missing_required_files(root), ['AGENTS.md'])
            self.assertFalse(is_workspace_ready(root))