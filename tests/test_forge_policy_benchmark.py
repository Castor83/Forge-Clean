import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts.benchmark_policy_service import MeasuredChat, execute_run, REPOSITORY
from src.forge_runtime.task_runner import parse_task_spec


class TestPolicyBenchmark(unittest.TestCase):
    def test_counts_repair_starts_separately_from_replacement_actions(self):
        with TemporaryDirectory(dir=REPOSITORY) as directory:
            client = Mock(return_value='{"action":"replace_text_batch"}')
            measured = MeasuredChat(client, Path(directory) / "trace.jsonl")
            initial = (("system", "rules"), ("user", json.dumps({"role": "builder"})))
            measured(initial)
            measured(initial + (("assistant", "response"), ("user", "feedback")))
            measured(initial + (("user", json.dumps({"kind": "host_check_feedback"})),))
            self.assertEqual(measured.builder_starts, 2)
            self.assertEqual(measured.model_calls, 3)
            self.assertEqual(measured.repair_actions, 3)

    def test_host_pass_cannot_turn_failed_pipeline_into_success(self):
        spec = parse_task_spec((REPOSITORY / "examples/forge_policy_service.json").read_text())
        with TemporaryDirectory(dir=REPOSITORY) as directory, patch(
            "scripts.benchmark_policy_service.run_forge_task", side_effect=RuntimeError("step limit")
        ), patch("scripts.benchmark_policy_service.run_host_suite", return_value=(True, "passed")):
            result = execute_run(Path(directory) / "run", spec, {"SPEC.md": b"fixture"}, Mock())
            self.assertFalse(result["success"])
            self.assertFalse(result["first_attempt_success"])
            self.assertIn("step limit", result["error"])
            self.assertTrue(result["independent_passed"])

    def test_existing_workspace_is_not_reused(self):
        with TemporaryDirectory(dir=REPOSITORY) as directory:
            with self.assertRaises(FileExistsError):
                execute_run(Path(directory), None, {}, Mock())


if __name__ == "__main__":
    unittest.main()
