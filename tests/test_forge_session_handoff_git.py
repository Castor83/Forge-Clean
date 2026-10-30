"""Independent behavioral acceptance of read-only Git observations."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from src.forge_runtime import session_handoff_git as observer


ROOT = Path(__file__).resolve().parents[1]
GIT = Path("C:/Program Files/Git/cmd/git.exe")


class GitObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspaces")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Isolated acceptance")
        self.git("config", "user.email", "acceptance@example.invalid")
        (self.root / "tracked.txt").write_text("original\n", encoding="utf-8")
        (self.root / "rename.txt").write_text("rename content\n", encoding="utf-8")
        self.git("add", "--", "tracked.txt", "rename.txt")
        self.git("commit", "-m", "isolated initial state")

    def git(self, *arguments):
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        result = subprocess.run([str(GIT), *arguments], cwd=self.root,
                                env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=True, timeout=15)
        return result.stdout

    def observe(self):
        return observer.observe_git(self.root, git_executable=GIT)

    def test_clean_fresh_and_detached(self):
        observation = self.observe()
        self.assertEqual(observation["version"], 1)
        self.assertEqual(Path(observation["repository_root"]), self.root)
        self.assertEqual(observation["branch"], "main")
        self.assertEqual(observation["head"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(observation["entries"], [])
        self.assertTrue(observer.git_is_fresh(self.root, observation, git_executable=GIT))
        self.git("checkout", "--detach")
        self.assertIsNone(self.observe()["branch"])
        self.assertFalse(observer.git_is_fresh(self.root, observation, git_executable=GIT))

    def test_complete_status_rename_unicode_and_read_only_effect(self):
        (self.root / "tracked.txt").write_text("modified\n", encoding="utf-8")
        (self.root / "staged.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "--", "staged.txt")
        self.git("mv", "rename.txt", "renamed ä.txt")
        (self.root / "untracked").mkdir()
        for filename in ("untracked/a.txt", "untracked/b.txt", "ä ungewöhnlich.txt"):
            (self.root / filename).write_text(filename, encoding="utf-8")
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        observation = self.observe()
        entries = {entry["path"]: entry for entry in observation["entries"]}
        self.assertEqual(entries["tracked.txt"]["status"], " M")
        self.assertEqual(entries["staged.txt"]["status"], "A ")
        self.assertEqual(entries["renamed ä.txt"]["status"], "R ")
        self.assertEqual(entries["renamed ä.txt"]["original_path"], "rename.txt")
        for filename in ("untracked/a.txt", "untracked/b.txt", "ä ungewöhnlich.txt"):
            self.assertEqual(entries[filename]["status"], "??")
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_stale_status_and_equal_status_does_not_claim_equal_contents(self):
        clean = self.observe()
        (self.root / "tracked.txt").write_text("changed once\n", encoding="utf-8")
        self.assertFalse(observer.git_is_fresh(self.root, clean, git_executable=GIT))
        dirty = self.observe()
        (self.root / "tracked.txt").write_text("changed twice\n", encoding="utf-8")
        self.assertTrue(observer.git_is_fresh(self.root, dirty, git_executable=GIT))

    def test_unborn_and_wrong_root_are_errors(self):
        child = self.root / "child"
        child.mkdir()
        with self.assertRaises(ValueError):
            observer.observe_git(child, git_executable=GIT)
        unborn = self.root / "unborn"
        unborn.mkdir()
        subprocess.run([str(GIT), "init", str(unborn)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self.assertRaises(ValueError):
            observer.observe_git(unborn, git_executable=GIT)
        with self.assertRaises(ValueError):
            observer.observe_git(self.root, git_executable="git")

    def test_environment_cannot_redirect_repository(self):
        expected = self.observe()
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "missing.git"),
                                     "GIT_WORK_TREE": str(self.root / "missing"),
                                     "GIT_INDEX_FILE": str(self.root / "wrong-index"),
                                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare",
                                     "GIT_CONFIG_VALUE_0": "true"}):
            self.assertEqual(self.observe(), expected)
        self.assertFalse((self.root / "wrong-index").exists())

    def test_local_fsmonitor_and_filter_helpers_never_execute(self):
        marker = self.root / "helper-executed"
        helper = self.root / "helper.py"
        helper.write_text("from pathlib import Path\nPath(" + repr(str(marker)) +
                          ").write_text('executed')\n", encoding="utf-8")
        command = '"' + sys.executable + '" "' + str(helper) + '"'
        self.git("config", "core.fsmonitor", command)
        self.observe()
        self.assertFalse(marker.exists(), "Fsmonitor must stay disabled")
        self.git("config", "filter.trap.clean", command)
        self.git("config", "filter.trap.smudge", command)
        (self.root / ".gitattributes").write_text("*.txt filter=trap\n", encoding="utf-8")
        (self.root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        self.assertFalse(marker.exists(), "Fixture must not run the helper before observation")
        with self.assertRaises(ValueError):
            self.observe()
        self.assertFalse(marker.exists())

    def test_changed_head_between_real_observations_is_rejected(self):
        original = observer._run_bounded
        head_reads = 0

        def changing(command, root, env, deadline):
            nonlocal head_reads
            result = original(command, root, env, deadline)
            if command[-3:] == ["rev-parse", "--verify", "HEAD"]:
                head_reads += 1
                if head_reads == 2:
                    return result[0], b"0" * 40 + b"\n", result[2]
            return result

        with patch.object(observer, "_run_bounded", side_effect=changing):
            with self.assertRaises(ValueError):
                self.observe()
        self.assertEqual(head_reads, 2)

    def test_metadata_redirection_files_are_rejected(self):
        for relative in ("commondir", "objects/info/alternates"):
            path = self.root / ".git" / relative
            path.write_text(str(self.root) + "\n", encoding="utf-8")
            try:
                with self.subTest(relative=relative), self.assertRaises(ValueError):
                    self.observe()
            finally:
                path.unlink()

    def test_gitdir_file_is_rejected_before_start(self):
        metadata = self.root / ".git"
        saved = self.root / "saved-metadata"
        metadata.rename(saved)
        metadata.write_text("gitdir: " + str(saved) + "\n", encoding="utf-8")
        with patch.object(observer, "_run_bounded") as run:
            with self.assertRaises(ValueError):
                self.observe()
            run.assert_not_called()

    def test_saved_observation_size_limits_and_branch_are_rejected(self):
        observation = self.observe()
        variants = [dict(observation, branch="bad branch"),
                    dict(observation, branch="a" * 2001),
                    dict(observation, repository_root="relative"),
                    dict(observation, entries=[{"status": "??", "path": "x" * 100001,
                                                "original_path": None}]),
                    dict(observation, entries=[{"status": "??", "path": "x",
                                                "original_path": None}] * 20001)]
        for variant in variants:
            with self.subTest(variant=list(variant)), patch.object(observer, "observe_git") as run:
                with self.assertRaises(ValueError):
                    observer.git_is_fresh(self.root, variant, git_executable=GIT)
                run.assert_not_called()

    def test_metadata_config_indirection_sections_are_rejected(self):
        config = self.root / ".git/config"
        initial = config.read_bytes()
        for section in ('[include]\n path = missing\n',
                        '[includeIf "gitdir:*"]\n path = missing\n',
                        '[extensions]\n objectformat = sha1\n'):
            config.write_bytes(initial + section.encode())
            with self.subTest(section=section), self.assertRaises(ValueError):
                self.observe()
        config.write_bytes(initial)


class GitBoundaryTests(unittest.TestCase):
    def test_parser_type_changes_and_unmerged_statuses(self):
        for status in ("TT", "T ", "TM", "TD", "DD", "AU", "UD", "UA", "DU", "AA", "UU"):
            with self.subTest(status=status):
                self.assertEqual(observer._parse_status((status + " file\0").encode()),
                                 [{"status": status, "path": "file", "original_path": None}])

    def test_invalid_saved_observation_is_rejected_before_process(self):
        for observation in ({}, {"version": True, "repository_root": str(ROOT),
                                 "branch": "main", "head": "0" * 40, "entries": []}):
            with self.subTest(observation=observation), patch.object(observer, "observe_git") as run:
                with self.assertRaises(ValueError):
                    observer.git_is_fresh(ROOT, observation, git_executable=GIT)
                run.assert_not_called()

    def test_parser_preserves_newline_and_rename_origin(self):
        result = observer._parse_status(b"?? odd\nname.txt\0R  new.txt\0old.txt\0")
        self.assertEqual(result, [
            {"status": "??", "path": "odd\nname.txt", "original_path": None},
            {"status": "R ", "path": "new.txt", "original_path": "old.txt"},
        ])

    def test_parser_rejects_malformed_bytes(self):
        for raw in (b"?? unterminated", b"?? \0", b"X  bad\0", b"?? \xff\0",
                    b"R  new\0", b"?? ok\0\0", b"? bad\0"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                observer._parse_status(raw)

    def test_real_stdout_stderr_overflow_and_timeout_fail(self):
        env = dict(os.environ)
        for stream in ("stdout", "stderr"):
            command = [sys.executable, "-c",
                       "import sys; sys." + stream + ".buffer.write(b'x'*100001)"]
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                observer._run_bounded(command, ROOT, env, time.monotonic() + 5)
        with self.assertRaises(ValueError):
            observer._run_bounded(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*60000); "
                 "sys.stderr.buffer.write(b'y'*60000)"],
                ROOT, env, time.monotonic() + 5)
        with self.assertRaises(ValueError):
            observer._run_bounded([sys.executable, "-c", "import time; time.sleep(10)"],
                                  ROOT, env, time.monotonic() + .15)

    def test_real_helper_success_returns_both_streams(self):
        result = observer._run_bounded(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            ROOT, dict(os.environ), time.monotonic() + 5)
        self.assertEqual(result, (0, b"out\r\n" if os.name == "nt" else b"out\n",
                                 b"err\r\n" if os.name == "nt" else b"err\n"))


if __name__ == "__main__":
    unittest.main()
