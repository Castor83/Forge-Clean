import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.contracts import SubagentTask
from src.forge_runtime.handoff import build_role_handoff
from src.forge_runtime.task_runner import parse_task_spec
from src.forge_runtime.workspace_tools import WorkspaceFileTools


class TestForgeReadOnlyFiles(unittest.TestCase):
    def test_task_rejects_overlap_duplicates_and_invalid_types(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            SubagentTask("task", "Build", ("SPEC.md",), ("SPEC.md",))
        with self.assertRaisesRegex(ValueError, "duplicates"):
            SubagentTask("task", "Build", ("agent.py",), ("SPEC.md", "SPEC.md"))
        with self.assertRaisesRegex(ValueError, "must be a tuple"):
            SubagentTask("task", "Build", ("agent.py",), ["SPEC.md"])

    def test_read_only_file_can_be_read_but_never_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SPEC.md").write_text("trusted specification", encoding="utf-8")
            task = SubagentTask(
                "task",
                "Build",
                ("agent.py",),
                ("SPEC.md",),
            )
            tools = WorkspaceFileTools(root, task)

            self.assertEqual(tools.read_text("SPEC.md"), "trusted specification")
            with self.assertRaisesRegex(ValueError, "not allowed to be written"):
                tools.write_text("SPEC.md", "model overwrite")
            self.assertEqual(
                (root / "SPEC.md").read_text(encoding="utf-8"),
                "trusted specification",
            )

    def test_handoff_identifies_read_only_files_separately(self):
        task = SubagentTask(
            "task",
            "Build",
            ("agent.py",),
            ("SPEC.md",),
        )

        handoff = json.loads(build_role_handoff("builder", task, ()))

        self.assertEqual(handoff["allowed_files"], ["agent.py"])
        self.assertEqual(handoff["read_only_files"], ["SPEC.md"])

    def test_task_parser_accepts_only_the_exact_optional_field(self):
        raw = {
            "task_id": "task",
            "objective": "Build from the specification",
            "allowed_files": ["agent.py"],
            "read_only_files": ["SPEC.md"],
            "checks": [{"kind": "file_exists", "path": "agent.py"}],
        }

        spec = parse_task_spec(json.dumps(raw))

        self.assertEqual(spec.task.read_only_files, ("SPEC.md",))
        raw["model_override"] = True
        with self.assertRaisesRegex(ValueError, "missing or extra"):
            parse_task_spec(json.dumps(raw))

    def test_windows_path_validation_also_applies_to_read_only_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                WorkspaceFileTools(
                    root,
                    SubagentTask("task", "Build", ("agent.py",), ("..\\SPEC.md",)),
                )

    def test_host_suite_may_use_a_read_only_dependency(self):
        raw = {
            "task_id": "task",
            "objective": "Build persistence around a verified codec",
            "allowed_files": ["agent/audit_log.py"],
            "read_only_files": ["agent/audit_chain.py"],
            "checks": [
                {"kind": "host_suite", "suite": "jsonl_audit_log_v1"}
            ],
        }

        spec = parse_task_spec(json.dumps(raw))

        self.assertEqual(spec.task.allowed_files, ("agent/audit_log.py",))
        self.assertEqual(
            spec.task.read_only_files,
            ("agent/audit_chain.py",),
        )


if __name__ == "__main__":
    unittest.main()
