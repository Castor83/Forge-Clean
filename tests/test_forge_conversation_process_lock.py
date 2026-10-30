import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_store import TaskStore


class ConversationProcessLockTests(unittest.TestCase):
    def test_other_process_is_blocked_and_owner_pid_is_visible_until_release(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            code = ("import sys\nfrom pathlib import Path\n"
                    "from src.forge_runtime.conversation_store import TaskStore\n"
                    "with TaskStore(Path(sys.argv[1])).exclusive():\n"
                    " print('LOCKED', flush=True)\n sys.stdin.readline()\n")
            process = subprocess.Popen([sys.executable, "-c", code, str(directory)],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            store = TaskStore(directory)
            lines = []
            reader = threading.Thread(target=lambda: lines.append(process.stdout.readline()), daemon=True)
            reader.start()
            try:
                reader.join(timeout=10)
                self.assertFalse(reader.is_alive(), "Child did not report readiness within 10 seconds")
                self.assertEqual(lines[0].strip(), "LOCKED")
                with self.assertRaises(RuntimeError) as caught:
                    with store.exclusive():
                        self.fail("Concurrent process opened the task")
                self.assertIn(str(process.pid), str(caught.exception))
                self.assertIn(str(store.lock_path), str(caught.exception))
                self.assertEqual(store.lock_path.read_text(), str(process.pid))
            finally:
                try:
                    process.communicate(input="\n", timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()  # Only this test's own bounded child process.
                    process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertFalse(store.lock_path.exists())
            with store.exclusive():
                self.assertTrue(store.lock_path.exists())
            self.assertFalse(store.lock_path.exists())

    def test_invalid_or_oversize_lock_content_is_not_echoed_or_removed(self):
        for content in (b"untrusted\x1b[2J", b"9" * 100_000):
            with self.subTest(size=len(content)), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder))
                store.lock_path.write_bytes(content)
                with self.assertRaises(RuntimeError) as caught:
                    with store.exclusive():
                        self.fail("Invalid lock ignored")
                message = str(caught.exception)
                self.assertIn(str(store.lock_path), message)
                self.assertIn("PID nicht lesbar", message)
                self.assertNotIn("\x1b", message)
                self.assertEqual(store.lock_path.read_bytes(), content)

    def test_unreadable_lock_still_blocks_without_deletion(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            store.lock_path.write_text("12345", encoding="ascii")
            with patch.object(Path, "open", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(RuntimeError, "PID nicht lesbar"):
                    with store.exclusive():
                        self.fail("Unreadable lock ignored")
            self.assertEqual(store.lock_path.read_text(encoding="ascii"), "12345")
