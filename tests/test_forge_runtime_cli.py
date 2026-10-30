import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.cli import main
from src.forge_runtime.contracts import PipelineResult, SubagentResult


ROOT = Path(__file__).resolve().parents[1]


def write_task(directory: Path) -> Path:
    path = directory / "task.json"
    path.write_text(
        json.dumps(
            {
                "task_id": "cli-test",
                "objective": "Create a harmless file",
                "allowed_files": ["agent.py"],
                "checks": [{"kind": "python_syntax", "path": "agent.py"}],
            }
        ),
        encoding="utf-8",
    )
    return path


class TestForgeRuntimeCli(unittest.TestCase):
    def test_approved_run_prints_report_and_returns_zero(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            workspace = Path(directory)
            task_file = write_task(workspace)
            result = PipelineResult(
                (
                    SubagentResult("builder", True, "Built", ()),
                    SubagentResult("tester", True, "Tested", ()),
                    SubagentResult("reviewer", True, "Reviewed", ()),
                )
            )
            output = io.StringIO()
            with (
                patch("src.forge_runtime.cli.LMStudioChatClient") as client,
                patch(
                    "src.forge_runtime.cli.run_forge_task", return_value=result
                ) as runner,
                contextlib.redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "--task-file",
                        str(task_file),
                        "--workspace",
                        str(workspace),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.call_args.kwargs["timeout_seconds"], 300.0)
        self.assertEqual(runner.call_args.kwargs["max_steps"], 20)
        self.assertEqual(runner.call_args.kwargs["max_call_seconds"], 300.0)
        self.assertEqual(runner.call_args.kwargs["max_elapsed_seconds"], 900.0)
        self.assertEqual(runner.call_args.kwargs["max_repair_attempts"], 1)
        self.assertTrue(json.loads(output.getvalue())["approved"])
        self.assertTrue(output.getvalue().endswith("\n"))

    def test_rejected_run_returns_two_without_false_success(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            workspace = Path(directory)
            task_file = write_task(workspace)
            result = PipelineResult(
                (SubagentResult("builder", False, "Rejected", ("reason",)),)
            )
            output = io.StringIO()
            with (
                patch("src.forge_runtime.cli.LMStudioChatClient"),
                patch("src.forge_runtime.cli.run_forge_task", return_value=result),
                contextlib.redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "--task-file",
                        str(task_file),
                        "--workspace",
                        str(workspace),
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertFalse(json.loads(output.getvalue())["approved"])

    def test_deadline_failure_is_returned_as_structured_error(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            workspace = Path(directory)
            task_file = write_task(workspace)
            error = io.StringIO()
            with (
                patch("src.forge_runtime.cli.LMStudioChatClient"),
                patch(
                    "src.forge_runtime.cli.run_forge_task",
                    side_effect=RuntimeError(
                        "model call deadline exceeded: role=builder, step=1"
                    ),
                ),
                contextlib.redirect_stderr(error),
            ):
                exit_code = main(
                    [
                        "--task-file",
                        str(task_file),
                        "--workspace",
                        str(workspace),
                    ]
                )

        self.assertEqual(exit_code, 1)
        report = json.loads(error.getvalue())
        self.assertEqual(report["status"], "error")
        self.assertIn("role=builder, step=1", report["error"])

    def test_rejects_paths_outside_repository_before_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            task_file = write_task(workspace)
            error = io.StringIO()
            with (
                patch("src.forge_runtime.cli.LMStudioChatClient") as client,
                contextlib.redirect_stderr(error),
            ):
                exit_code = main(
                    [
                        "--task-file",
                        str(task_file),
                        "--workspace",
                        str(workspace),
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(error.getvalue())["status"], "error")
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
