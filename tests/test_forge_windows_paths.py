import ctypes
import os
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from src.forge_runtime.contracts import SubagentTask
from src.forge_runtime.workspace_tools import WorkspaceFileTools


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows path hardening tests")
class TestForgeWindowsPaths(unittest.TestCase):
    def make_tools(self, root: Path, allowed_files=("src\\agent.py",)):
        task = SubagentTask("windows-paths", "Edit approved files", allowed_files)
        return WorkspaceFileTools(root, task)

    @contextmanager
    def directory_junction(self, link: Path, target: Path):
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            self.fail(f"could not create test junction: {result.stderr or result.stdout}")
        try:
            yield
        finally:
            link.rmdir()

    def test_junction_escape_is_rejected_during_initialization(self):
        with TemporaryDirectory() as root_directory, TemporaryDirectory() as outside:
            root = Path(root_directory)
            outside_root = Path(outside)
            (outside_root / "secret.txt").write_text("outside", encoding="utf-8")
            link = root / "junction"

            with self.directory_junction(link, outside_root):
                with self.assertRaisesRegex(ValueError, "within the workspace"):
                    self.make_tools(root, ("junction\\secret.txt",))

    def test_junction_swap_after_initialization_blocks_read_and_write(self):
        with TemporaryDirectory() as root_directory, TemporaryDirectory() as outside:
            root = Path(root_directory)
            approved_directory = root / "approved"
            approved_directory.mkdir()
            approved_path = approved_directory / "agent.py"
            approved_path.write_text("inside", encoding="utf-8")
            tools = self.make_tools(root, ("approved\\agent.py",))

            approved_path.unlink()
            approved_directory.rmdir()
            outside_path = Path(outside) / "agent.py"
            outside_path.write_text("outside", encoding="utf-8")

            with self.directory_junction(approved_directory, Path(outside)):
                with self.assertRaisesRegex(
                    ValueError, "within the workspace|not allowed"
                ):
                    tools.read_text("approved\\agent.py")
                with self.assertRaisesRegex(
                    ValueError, "within the workspace|not allowed"
                ):
                    tools.write_text("approved\\agent.py", "changed")
                self.assertEqual(outside_path.read_text(encoding="utf-8"), "outside")

    def test_symlink_escape_is_rejected_when_supported(self):
        with TemporaryDirectory() as root_directory, TemporaryDirectory() as outside:
            root = Path(root_directory)
            outside_path = Path(outside) / "secret.txt"
            outside_path.write_text("outside", encoding="utf-8")
            link = root / "linked.txt"
            try:
                link.symlink_to(outside_path)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "within the workspace"):
                self.make_tools(root, ("linked.txt",))

    def test_drive_unc_rooted_and_namespace_paths_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (
                "C:relative.py",
                "C:\\absolute.py",
                "\\Windows\\system.ini",
                "\\\\server\\share\\file.py",
                "\\\\?\\C:\\temp\\file.py",
            )
            for path in paths:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, "within the workspace"):
                        self.make_tools(root, (path,))

    def test_alias_spellings_do_not_match_an_allowed_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "Source" / "Agent.py"
            path.parent.mkdir()
            path.write_text("approved", encoding="utf-8")
            tools = self.make_tools(root, ("Source\\Agent.py",))

            aliases = (
                "source\\agent.py",
                "Source/Agent.py",
                "Source\\.\\Agent.py",
            )
            for alias in aliases:
                with self.subTest(alias=alias):
                    with self.assertRaisesRegex(ValueError, "not allowed"):
                        tools.read_text(alias)

    def test_8dot3_alias_does_not_match_long_allowed_path_when_available(self):
        with TemporaryDirectory(dir=_REPOSITORY_ROOT) as directory:
            root = Path(directory)
            long_directory = root / "Long Directory Name"
            long_directory.mkdir()
            (long_directory / "agent.py").write_text("approved", encoding="utf-8")
            short_directory = _short_path(long_directory)
            short_name = Path(short_directory).name
            if short_name.casefold() == long_directory.name.casefold():
                self.skipTest("8.3 short names are disabled on this test volume")

            tools = self.make_tools(root, ("Long Directory Name\\agent.py",))
            with self.assertRaisesRegex(ValueError, "not allowed"):
                tools.read_text(f"{short_name}\\agent.py")

    def test_ambiguous_windows_file_names_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (
                "agent.py:payload",
                "NUL",
                "CON.txt",
                "CON .txt",
                "COM1.log",
                "src\\agent?.py",
                "src\\agent\x01.py",
                "src\\agent.py.",
                "src\\agent.py ",
            )
            for path in paths:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, "Windows path"):
                        self.make_tools(root, (path,))


def _short_path(path: Path) -> str:
    get_short_path = ctypes.windll.kernel32.GetShortPathNameW
    get_short_path.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    get_short_path.restype = ctypes.c_uint32
    required = get_short_path(str(path), None, 0)
    if required == 0:
        raise ctypes.WinError()
    buffer = ctypes.create_unicode_buffer(required)
    if get_short_path(str(path), buffer, required) == 0:
        raise ctypes.WinError()
    return buffer.value


if __name__ == "__main__":
    unittest.main()
