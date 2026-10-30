import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from unittest.mock import patch

from src.forge_runtime import HOST_ONLY_DECISIONS, HostOnlyDecision
from src.forge_runtime.contracts import PipelineResult, SubagentResult, SubagentTask
from src.forge_runtime.pipeline import run_review_pipeline
from src.forge_runtime.tool_executor import ForgeToolExecutor
from src.forge_runtime.tool_loop import parse_tool_action, run_tool_loop
from src.forge_runtime.workspace_tools import WorkspaceFileTools


class TestForgeHostPolicy(unittest.TestCase):
    def test_catalog_is_immutable_and_complete(self):
        expected = {
            "task_schema",
            "workspace_root",
            "allowed_files",
            "read_only_files",
            "role_tool_permissions",
            "resource_limits",
            "check_definitions",
            "host_suite_registry",
            "check_results",
            "repair_feedback",
            "generated_code_eligibility",
            "model_endpoint",
            "role_transition",
            "task_success",
        }
        self.assertIsInstance(HOST_ONLY_DECISIONS, frozenset)
        self.assertEqual({decision.value for decision in HOST_ONLY_DECISIONS}, expected)
        self.assertEqual(HOST_ONLY_DECISIONS, frozenset(HostOnlyDecision))

    def test_role_transition_is_bound_to_registered_host_decision(self):
        task = SubagentTask("task", "objective", ("result.txt",))
        result = SubagentResult("builder", False, "stopped", ())

        with patch(
            "src.forge_runtime.pipeline.host_decision", return_value=False
        ) as decide:
            with self.assertRaisesRegex(ValueError, "expected SubagentResult role"):
                run_review_pipeline(task, lambda *_args: result)

        decide.assert_called_once_with(HostOnlyDecision.ROLE_TRANSITION, True)

    def test_task_success_is_bound_to_registered_host_decision(self):
        results = (
            SubagentResult("builder", True, "built", ()),
            SubagentResult("tester", True, "tested", ()),
            SubagentResult("reviewer", True, "reviewed", ()),
        )

        with patch(
            "src.forge_runtime.contracts.host_decision", return_value=False
        ) as decide:
            self.assertFalse(PipelineResult(results).approved)

        decide.assert_called_once_with(HostOnlyDecision.TASK_SUCCESS, True)

    def test_repair_feedback_is_bound_to_registered_host_decision(self):
        with TemporaryDirectory() as directory:
            executor = ForgeToolExecutor(Path(directory), lambda _messages: "")
            with patch(
                "src.forge_runtime.tool_executor.host_decision",
                return_value=False,
            ) as decide:
                with self.assertRaisesRegex(ValueError, "repair feedback"):
                    executor.set_builder_repair_feedback("fixed host finding")

        decide.assert_called_once_with(HostOnlyDecision.REPAIR_FEEDBACK, True)

    def test_model_cannot_add_policy_fields_to_an_action(self):
        response = json.dumps(
            {
                "action": "finish",
                "passed": True,
                "summary": "claim",
                "findings": [],
                "allowed_files": ["outside.txt"],
            }
        )
        with self.assertRaisesRegex(ValueError, "exactly action"):
            parse_tool_action(response)

    def test_model_cannot_grant_tester_write_permission(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("task", "objective", ("result.txt",))
            workspace = WorkspaceFileTools(root, task)
            previous_results = (
                SubagentResult("builder", True, "built", ()),
            )
            responses = iter(
                [
                    json.dumps(
                        {
                            "action": "write_file",
                            "path": "result.txt",
                            "content": "x",
                        }
                    )
                ]
            )

            with self.assertRaisesRegex(ValueError, "only builder may write"):
                run_tool_loop(
                    "tester",
                    task,
                    previous_results,
                    workspace,
                    lambda _messages: next(responses),
                )

            self.assertFalse((root / "result.txt").exists())

    def test_model_cannot_replace_host_check_result(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("task", "objective", ("result.txt",))
            workspace = WorkspaceFileTools(root, task)
            previous_results = (
                SubagentResult("builder", True, "built", ()),
                SubagentResult("tester", True, "tested", ()),
            )
            responses = iter(
                (
                    json.dumps({"action": "run_check"}),
                    json.dumps(
                        {
                            "action": "finish",
                            "passed": True,
                            "summary": "I declare success",
                            "findings": [],
                        }
                    ),
                )
            )

            with self.assertRaisesRegex(ValueError, "successful host check"):
                run_tool_loop(
                    "reviewer",
                    task,
                    previous_results,
                    workspace,
                    lambda _messages: next(responses),
                    check_runner=lambda: (False, "failed"),
                )


if __name__ == "__main__":
    unittest.main()
