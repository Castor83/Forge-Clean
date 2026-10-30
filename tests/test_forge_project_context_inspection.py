"""Behavioral proof that inspection reads history without exercising authority."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime import conversation_cli, project_inspection
from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.project_session import MARKER_FILENAME, REVOCATION_FILENAME
from tests.test_forge_conversation_build import actions, ready


OBSERVATION_KEYS = {"conversation", "project_context", "binding_marker", "revocation", "lock", "build_file"}
OBSERVATION_FIELDS = {"name", "status", "size", "sha256", "changed_during_read", "data"}


class ProjectContextInspectionTests(unittest.TestCase):
    @contextmanager
    def workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "workspaces"
            directory = base / "inspection_task"
            directory.mkdir(parents=True)
            with patch.object(project_inspection, "WORKSPACES_ROOT", base), \
                    patch.object(conversation_cli, "__file__", str(root / "src/forge_runtime/conversation_cli.py")):
                yield root, directory

    def prepare(self, directory, state="ready"):
        store = TaskStore(directory)
        with store.exclusive():
            task = ConversationTask(store)
            task.initialize_project("inspection_project")
            ready(task)
            if state == "completed":
                self.assertTrue(build_confirmed(task, actions(), lambda _: None))
            elif state == "cancelled":
                task.cancel()
            elif state == "running":
                task.change(state="running", event="Isolierte gespeicherte Laufzustandsprobe.")
        (directory / "user_note.txt").write_bytes(b"preserve unrelated user data")
        return store

    def snapshot(self, directory):
        return {path.relative_to(directory).as_posix(): path.read_bytes() if path.is_file() else None
                for path in directory.rglob("*")}

    @contextmanager
    def no_execution(self):
        targets = (
            "src.forge_runtime.conversation_store.TaskStore.__init__",
            "src.forge_runtime.conversation_store.ConversationTask.__init__",
            "src.forge_runtime.conversation_cli.LMStudioChatClient",
            "src.forge_runtime.conversation_cli.check_completed",
            "src.forge_runtime.task_runner.verify_forge_task",
            "src.forge_runtime.conversation_build.verify_forge_task",
            "src.forge_runtime.task_runner.exec",
        )
        with ExitStack() as stack:
            spies = [stack.enter_context(patch(target, side_effect=AssertionError("inspection exercised " + target)))
                     for target in targets]
            yield
            for spy in spies:
                spy.assert_not_called()

    def assert_historical_report(self, report, directory):
        self.assertEqual(set(report), {"inspection_only", "current_approval", "snapshot_consistency",
                                      "workspace", "problems", "observations"})
        self.assertTrue(report["inspection_only"])
        self.assertFalse(report["current_approval"])
        self.assertEqual(report["snapshot_consistency"], "not_established")
        self.assertEqual(Path(report["workspace"]), directory.resolve())
        self.assertTrue(Path(report["workspace"]).is_absolute())
        self.assertIsInstance(report["problems"], list)
        self.assertEqual(set(report["observations"]), OBSERVATION_KEYS)
        for observation in report["observations"].values():
            self.assertEqual(set(observation), OBSERVATION_FIELDS)

    def test_every_saved_state_is_read_only_even_with_lock_and_broken_project_files(self):
        for state in ("completed", "running", "cancelled", "ready"):
            for damaged in (None, "context", "lock", "revocation"):
                with self.subTest(state=state, damaged=damaged), self.workspace() as (_, directory):
                    self.prepare(directory, state)
                    (directory / "conversation.lock").write_bytes(b"424242")
                    (directory / REVOCATION_FILENAME).write_text(
                        json.dumps({"version": 1, "project_id": "inspection_project"}), encoding="utf-8")
                    if damaged is not None:
                        name = {"context": "project_context.json", "lock": "conversation.lock",
                                "revocation": REVOCATION_FILENAME}[damaged]
                        (directory / name).write_bytes(b"broken data")
                    before = self.snapshot(directory)
                    with self.no_execution():
                        report = project_inspection.inspect_project_store(directory)
                    self.assertEqual(self.snapshot(directory), before)
                    self.assert_historical_report(report, directory)
                    self.assertEqual(report["observations"]["conversation"]["data"]["state"], state)
                    self.assertTrue(report["problems"])
                    if damaged is not None:
                        key = {"context": "project_context", "lock": "lock", "revocation": "revocation"}[damaged]
                        self.assertEqual(report["observations"][key]["status"], "invalid")
                    if state == "completed":
                        self.assertTrue(report["observations"]["conversation"]["data"]["report"]["approved"])
                        self.assertEqual(report["observations"]["build_file"]["data"],
                                         "def greeting(name):\n    return 'Hallo ' + name\n")

    def test_valid_completed_inspection_is_still_not_a_current_approval(self):
        with self.workspace() as (_, directory):
            self.prepare(directory, "completed")
            before = self.snapshot(directory)
            with self.no_execution():
                report = project_inspection.inspect_project_store(directory)
            self.assert_historical_report(report, directory)
            self.assertEqual(report["problems"], [])
            self.assertTrue(report["observations"]["conversation"]["data"]["report"]["approved"])
            self.assertEqual(self.snapshot(directory), before)

    def test_traversing_stored_proposal_cannot_read_a_foreign_sentinel(self):
        with self.workspace() as (root, directory):
            store = self.prepare(directory)
            sentinel = root / "foreign_secret.txt"
            sentinel.write_bytes(b"must not be opened")
            data = json.loads(store.path.read_text(encoding="utf-8"))
            data["proposal"]["allowed_files"] = ["../../../foreign_secret.txt"]
            data["proposal"]["checks"][0]["path"] = "../../../foreign_secret.txt"
            store.path.write_text(json.dumps(data), encoding="utf-8")
            before = self.snapshot(root)
            opened = []
            original_open = Path.open

            def tracked_open(path, *args, **kwargs):
                opened.append(path)
                return original_open(path, *args, **kwargs)

            with self.no_execution(), patch.object(Path, "open", tracked_open):
                report = project_inspection.inspect_project_store(directory)
            self.assertNotIn(sentinel, opened)
            self.assertEqual(report["observations"]["conversation"]["status"], "invalid")
            self.assertIsNone(report["observations"]["build_file"]["data"])
            self.assertEqual(self.snapshot(root), before)

    def test_each_oversized_artifact_is_rejected_before_it_is_opened(self):
        artifacts = (("conversation", "conversation.json", 1_000_000),
                     ("project_context", "project_context.json", 100_000),
                     ("binding_marker", MARKER_FILENAME, 1024),
                     ("revocation", REVOCATION_FILENAME, 1024),
                     ("lock", "conversation.lock", 64),
                     ("build_file", "build/agent/hello.py", 80_000))
        for key, name, maximum in artifacts:
            with self.subTest(name=name), self.workspace() as (_, directory):
                self.prepare(directory)
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x" * (maximum + 1))
                before = self.snapshot(directory)
                opened = []
                original_open = Path.open

                def tracked_open(path, *args, **kwargs):
                    opened.append(path)
                    return original_open(path, *args, **kwargs)

                with self.no_execution(), patch.object(Path, "open", tracked_open):
                    report = project_inspection.inspect_project_store(directory)
                self.assertNotIn(target, opened)
                self.assertEqual(report["observations"][key]["status"], "invalid")
                self.assertTrue(report["problems"])
                self.assertEqual(self.snapshot(directory), before)

    def test_file_and_build_ancestor_links_are_blocked_before_reading_selected_file(self):
        for method in ("is_symlink", "is_junction"):
            for relative in ("project_context.json", "build", "build/agent/hello.py"):
                with self.subTest(method=method, relative=relative), self.workspace() as (_, directory):
                    self.prepare(directory, "completed")
                    linked = directory / relative
                    target = directory / ("project_context.json" if relative == "project_context.json" else "build/agent/hello.py")
                    opened = []
                    original_open = Path.open

                    def tracked_open(path, *args, **kwargs):
                        opened.append(path)
                        return original_open(path, *args, **kwargs)

                    before = self.snapshot(directory)
                    with self.no_execution(), patch.object(Path, method, autospec=True,
                            side_effect=lambda path: path == linked), patch.object(Path, "open", tracked_open):
                        report = project_inspection.inspect_project_store(directory)
                    key = "project_context" if relative == "project_context.json" else "build_file"
                    self.assertEqual(report["observations"][key]["status"], "blocked")
                    self.assertNotIn(target, opened)
                    self.assertEqual(self.snapshot(directory), before)

    def test_workspace_ancestor_link_prevents_every_file_open(self):
        for method in ("is_symlink", "is_junction"):
            with self.subTest(method=method), self.workspace() as (root, directory):
                self.prepare(directory)
                before = self.snapshot(directory)
                with self.no_execution(), patch.object(Path, method, autospec=True,
                        side_effect=lambda path: path == root), patch.object(Path, "open") as opened:
                    with self.assertRaises(ValueError):
                        project_inspection.inspect_project_store(directory)
                    opened.assert_not_called()
                self.assertEqual(self.snapshot(directory), before)

    def test_foreign_directory_is_rejected_before_any_filesystem_inspection(self):
        with self.workspace() as (root, directory):
            foreign = root / "foreign"
            escaped = directory / ".." / ".." / "foreign"
            for target in (foreign, escaped, directory.parent):
                with self.subTest(target=target), patch.object(project_inspection, "_reject_links") as links, \
                        patch.object(Path, "resolve") as resolved, patch.object(Path, "open") as opened:
                    with self.assertRaises(ValueError):
                        project_inspection.inspect_project_store(target)
                    links.assert_not_called()
                    resolved.assert_not_called()
                    opened.assert_not_called()

    def test_cli_inspection_uses_read_only_path_and_does_not_create_unknown_workspace(self):
        with self.workspace() as (_, directory):
            self.prepare(directory, "completed")
            before = self.snapshot(directory)
            output = io.StringIO()
            with self.no_execution(), redirect_stdout(output):
                self.assertEqual(conversation_cli.main(["--workspace", str(directory), "--inspect-project"]), 0)
            self.assertIn('"current_approval": false', output.getvalue())
            self.assertIn('"snapshot_consistency": "not_established"', output.getvalue())
            self.assertEqual(self.snapshot(directory), before)
            absent = directory.parent / "absent" / "child"
            with self.no_execution(), redirect_stdout(io.StringIO()):
                self.assertEqual(conversation_cli.main(["--workspace", str(absent), "--inspect-project"]), 1)
            self.assertFalse(absent.parent.exists())
            self.assertEqual(self.snapshot(directory), before)

    def test_cli_rejects_inspection_and_recovery_together_before_creating_any_directory(self):
        with self.workspace() as (root, directory):
            absent = directory.parent / "absent"
            before = self.snapshot(root)
            with self.no_execution(), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                conversation_cli.main(["--workspace", str(absent), "--inspect-project",
                                       "--recover-project-id", "inspection_project"])
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(absent.exists())
            self.assertEqual(self.snapshot(root), before)

    def test_cli_can_inspect_a_locked_broken_order_without_changing_it(self):
        with self.workspace() as (_, directory):
            self.prepare(directory, "completed")
            (directory / "conversation.lock").write_bytes(b"424242")
            (directory / "project_context.json").write_bytes(b"broken")
            (directory / REVOCATION_FILENAME).write_text(
                json.dumps({"version": 1, "project_id": "inspection_project"}), encoding="utf-8")
            before = self.snapshot(directory)
            output = io.StringIO()
            with self.no_execution(), redirect_stdout(output):
                self.assertEqual(conversation_cli.main(["--workspace", str(directory), "--inspect-project"]), 1)
            self.assertEqual(self.snapshot(directory), before)
            self.assertIn("Sperrdatei vorhanden", output.getvalue())
            self.assertIn('"current_approval": false', output.getvalue())
            self.assertIn('"approved": true', output.getvalue())

    def test_file_growth_after_size_check_uses_one_bounded_read_and_rejects_oversize(self):
        with self.workspace() as (_, directory):
            self.prepare(directory)
            target = directory / "project_context.json"
            before = target.read_bytes()
            original_open = Path.open
            requested = []

            class BoundedReader:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self.stream.close()

                def fileno(self):
                    return self.stream.fileno()

                def read(self, maximum):
                    requested.append(maximum)
                    return self.stream.read(maximum)

            def grow_at_open(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                if path == target and args == ("rb",):
                    target.write_bytes(before + b" " * 100_001)
                    return BoundedReader(stream)
                return stream

            with self.no_execution(), patch.object(Path, "open", grow_at_open):
                report = project_inspection.inspect_project_store(directory)
            self.assertEqual(requested, [100_001])
            observation = report["observations"]["project_context"]
            self.assertEqual(observation["status"], "invalid")
            self.assertIsNone(observation["data"])
            self.assertTrue(observation["changed_during_read"])
            self.assert_historical_report(report, directory)

    def test_change_during_read_is_reported_without_claiming_snapshot_consistency(self):
        with self.workspace() as (_, directory):
            self.prepare(directory, "completed")
            target = directory / "conversation.json"
            original_bytes = target.read_bytes()
            original_open = Path.open

            class MutatingReader:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self.stream.close()

                def fileno(self):
                    return self.stream.fileno()

                def read(self, maximum):
                    result = self.stream.read(maximum)
                    target.write_bytes(original_bytes + b" ")
                    return result

            def mutate_on_read(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                return MutatingReader(stream) if path == target and args == ("rb",) else stream

            with self.no_execution(), patch.object(Path, "open", mutate_on_read):
                report = project_inspection.inspect_project_store(directory)
            observation = report["observations"]["conversation"]
            self.assertTrue(observation["changed_during_read"])
            self.assertEqual(observation["status"], "changed")
            self.assertEqual(observation["sha256"], hashlib.sha256(original_bytes).hexdigest())
            self.assert_historical_report(report, directory)
            self.assertTrue(report["problems"])


if __name__ == "__main__":
    unittest.main()
