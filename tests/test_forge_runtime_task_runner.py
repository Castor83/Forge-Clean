import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.contracts import PipelineResult, SubagentResult, SubagentTask
from src.forge_runtime.task_runner import (
    FunctionCase,
    ForgeTaskSpec,
    StaticCheck,
    format_run_report,
    parse_task_spec,
    run_forge_task,
)


class ScriptedChat:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, messages):
        return self.responses.pop(0)


def task_json(checks=None):
    return json.dumps(
        {
            "task_id": "safe-agent",
            "objective": "Create a small harmless validator and documentation",
            "allowed_files": ["agent/validator.py", "README.md"],
            "checks": checks
            or [
                {"kind": "python_syntax", "path": "agent/validator.py"},
                {
                    "kind": "contains_text",
                    "path": "agent/validator.py",
                    "text": "def validate",
                },
                {
                    "kind": "python_function_cases",
                    "path": "agent/validator.py",
                    "function": "validate",
                    "cases": [
                        {"args": ["text"], "expected": True},
                        {"args": [""], "expected": False},
                    ],
                },
                {"kind": "file_exists", "path": "README.md"},
            ],
        }
    )


class TestTaskSpecification(unittest.TestCase):
    def test_parses_complete_specification(self):
        spec = parse_task_spec(task_json())

        self.assertEqual(spec.task.task_id, "safe-agent")
        self.assertEqual(
            spec.task.allowed_files,
            ("agent/validator.py", "README.md"),
        )
        self.assertEqual(
            tuple(check.kind for check in spec.checks),
            (
                "python_syntax",
                "contains_text",
                "python_function_cases",
                "file_exists",
            ),
        )

    def test_rejects_invalid_shape_types_and_coverage(self):
        cases = (
            "not json",
            json.dumps({}),
            json.dumps(
                {
                    "task_id": "x",
                    "objective": "x",
                    "allowed_files": "x.py",
                    "checks": [],
                }
            ),
            task_json([{"kind": "unknown", "path": "agent/validator.py"}]),
            task_json([{"kind": "file_exists", "path": "other.py"}]),
            task_json([{"kind": "file_exists", "path": "agent/validator.py"}]),
            task_json(
                [
                    {
                        "kind": "contains_text",
                        "path": "agent/validator.py",
                        "text": "",
                    },
                    {"kind": "file_exists", "path": "README.md"},
                ]
            ),
        )
        for content in cases:
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    parse_task_spec(content)

    def test_rejects_subclasses_and_duplicate_allowed_files(self):
        class TaskSubclass(SubagentTask):
            pass

        class CheckSubclass(StaticCheck):
            pass

        class CaseSubclass(FunctionCase):
            pass

        task = SubagentTask("task", "objective", ("file.py",))
        with self.assertRaises(ValueError):
            ForgeTaskSpec(TaskSubclass("task", "objective", ("file.py",)), ())
        with self.assertRaises(ValueError):
            ForgeTaskSpec(task, (CheckSubclass("file_exists", "file.py"),))
        with self.assertRaises(ValueError):
            StaticCheck(
                "python_function_cases",
                "file.py",
                function="answer",
                cases=(CaseSubclass((), True),),
            )
        with self.assertRaisesRegex(ValueError, "duplicates"):
            ForgeTaskSpec(
                SubagentTask("task", "objective", ("file.py", "file.py")),
                (StaticCheck("file_exists", "file.py"),),
            )


class TestForgeTaskRunner(unittest.TestCase):
    def test_runs_two_file_pipeline_with_fixed_static_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = parse_task_spec(task_json())
            chat = ScriptedChat(
                (
                    '{"action":"write_file","path":"agent/validator.py",'
                    '"content":"def validate(value):\\n    return bool(value)\\n"}',
                    '{"action":"write_file","path":"README.md",'
                    '"content":"# Validator\\n"}',
                    '{"action":"finish","passed":true,"summary":"Built",'
                    '"findings":["Two files created"]}',
                    '{"action":"read_file","path":"agent/validator.py"}',
                    '{"action":"read_file","path":"README.md"}',
                    '{"action":"run_check"}',
                    '{"action":"finish","passed":true,"summary":"Tested",'
                    '"findings":["Static checks passed"]}',
                    '{"action":"read_file","path":"agent/validator.py"}',
                    '{"action":"read_file","path":"README.md"}',
                    '{"action":"run_check"}',
                    '{"action":"finish","passed":true,"summary":"Reviewed",'
                    '"findings":["Approved"]}',
                )
            )

            result = run_forge_task(root, spec, chat)

            self.assertTrue(result.approved)
            self.assertEqual(
                (root / "agent" / "validator.py").read_text(encoding="utf-8"),
                "def validate(value):\n    return bool(value)\n",
            )

    def test_failed_static_check_cannot_be_reported_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = ForgeTaskSpec(
                SubagentTask("task", "Create Python", ("agent.py",)),
                (StaticCheck("python_syntax", "agent.py"),),
            )
            chat = ScriptedChat(
                (
                    '{"action":"write_file","path":"agent.py",'
                    '"content":"def broken(:\\n"}',
                    '{"action":"finish","passed":true,"summary":"Built",'
                    '"findings":[]}',
                    '{"action":"read_file","path":"agent.py"}',
                    '{"action":"run_check"}',
                    '{"action":"finish","passed":true,"summary":"Tested",'
                    '"findings":[]}',
                )
            )

            with self.assertRaisesRegex(ValueError, "successful host check"):
                run_forge_task(root, spec, chat)

    def test_restricted_function_cases_reject_unsafe_generated_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = ForgeTaskSpec(
                SubagentTask("task", "Create a pure function", ("agent.py",)),
                (
                    StaticCheck(
                        "python_function_cases",
                        "agent.py",
                        function="answer",
                        cases=(FunctionCase((), 42),),
                    ),
                ),
            )
            chat = ScriptedChat(
                (
                    '{"action":"write_file","path":"agent.py",'
                    '"content":"import os\\ndef answer():\\n    return 42\\n"}',
                    '{"action":"finish","passed":true,"summary":"Built",'
                    '"findings":[]}',
                    '{"action":"read_file","path":"agent.py"}',
                    '{"action":"run_check"}',
                    '{"action":"finish","passed":true,"summary":"Tested",'
                    '"findings":[]}',
                )
            )

            with self.assertRaisesRegex(ValueError, "successful host check"):
                run_forge_task(root, spec, chat)

    def test_restricted_function_cases_reject_attribute_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = ForgeTaskSpec(
                SubagentTask("task", "Create a pure function", ("agent.py",)),
                (
                    StaticCheck(
                        "python_function_cases",
                        "agent.py",
                        function="inspect_value",
                        cases=(FunctionCase(("text",), "text"),),
                    ),
                ),
            )
            chat = ScriptedChat(
                (
                    '{"action":"write_file","path":"agent.py",'
                    '"content":"def inspect_value(value):\\n'
                    '    return value.__class__\\n"}',
                    '{"action":"finish","passed":true,"summary":"Built",'
                    '"findings":[]}',
                    '{"action":"read_file","path":"agent.py"}',
                    '{"action":"run_check"}',
                    '{"action":"finish","passed":true,"summary":"Tested",'
                    '"findings":[]}',
                )
            )

            with self.assertRaisesRegex(ValueError, "successful host check"):
                run_forge_task(root, spec, chat)

    def test_formats_deterministic_report(self):
        result = PipelineResult(
            (
                SubagentResult("builder", True, "Built", ()),
                SubagentResult("tester", True, "Tested", ("ok",)),
                SubagentResult("reviewer", True, "Reviewed", ()),
            )
        )

        report = format_run_report(result)

        self.assertEqual(
            report,
            '{"approved":true,"results":['
            '{"findings":[],"passed":true,"role":"builder","summary":"Built"},'
            '{"findings":["ok"],"passed":true,"role":"tester",'
            '"summary":"Tested"},'
            '{"findings":[],"passed":true,"role":"reviewer",'
            '"summary":"Reviewed"}]}\n',
        )
        self.assertFalse(report.endswith("\n\n"))


if __name__ == "__main__":
    unittest.main()
