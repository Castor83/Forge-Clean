import ast
import unittest
from unittest.mock import Mock, patch

from src.forge_runtime.task_runner import (
    FunctionCase, StaticCheck, _load_restricted_function,
    _run_restricted_function_cases,
)


def load(expression):
    return _load_restricted_function(
        ast.parse("def probe(value):\n    return " + expression + "\n"), "probe")


class RestrictedExpressionResourcesTests(unittest.TestCase):
    def test_repeated_addition_is_rejected_before_generated_function_runs(self):
        generated = Mock()

        def fake_exec(code, namespace):
            namespace["probe"] = generated

        with patch("src.forge_runtime.task_runner.exec", side_effect=fake_exec):
            function = load(" + ".join(["value"] * 128))
        with self.assertRaisesRegex(ValueError, "value resource limit"):
            function("x" * 1024)
        generated.assert_not_called()

    def test_budget_boundary_allows_real_result_and_blocks_next_character(self):
        function = load("value + value")
        self.assertEqual(function("x" * 32768), "x" * 65536)
        with self.assertRaisesRegex(ValueError, "resource limit"):
            function("x" * 32769)

    def test_formatted_repetition_and_inactive_branches_are_bounded(self):
        for expression in ('f"{value}{value}"', '(value + value) or "ok"'):
            with self.subTest(expression=expression):
                with self.assertRaisesRegex(ValueError, "resource limit"):
                    load(expression)("x" * 32769)

    def test_literal_growth_is_rejected_before_compilation(self):
        tree = ast.parse('def probe(value):\n    return ' + repr("x" * 32769)
                         + ' + ' + repr("x" * 32769))
        with patch("src.forge_runtime.task_runner.compile") as compiler:
            with self.assertRaisesRegex(ValueError, "resource limit"):
                _load_restricted_function(tree, "probe")
        compiler.assert_not_called()

    def test_formatted_builtin_names_also_consume_budget(self):
        self.assertEqual(load('f"{str}"')(None), "<class 'str'>")
        with self.assertRaisesRegex(ValueError, "resource limit"):
            load('f"' + '{isinstance}' * 600 + '"')

    def test_large_integer_and_non_scalar_reject_without_custom_methods(self):
        function = load("value + value")
        with self.assertRaisesRegex(ValueError, "integer resource limit"):
            function(1 << 4096)
        custom = Mock()
        with self.assertRaisesRegex(ValueError, "finite JSON scalars"):
            function(custom)
        self.assertEqual(custom.mock_calls, [])
        self.assertEqual(function(12), 24)

    def test_many_independent_nodes_reject_before_compilation(self):
        tree = ast.parse('def probe(value):\n    return ' + ' or '.join(['value'] * 5100))
        with patch("src.forge_runtime.task_runner.compile") as compiler:
            with self.assertRaisesRegex(ValueError, "expression resource limit"):
                _load_restricted_function(tree, "probe")
        compiler.assert_not_called()

    def test_host_case_reports_resource_rejection_as_failure(self):
        check = StaticCheck("python_function_cases", "agent/probe.py", function="probe",
                            cases=(FunctionCase(("x" * 32769,), "unused"),))
        self.assertEqual(_run_restricted_function_cases(
            "def probe(value):\n    return value + value\n", check), "case 1 raised ValueError")

    def test_small_greeting_strip_and_boolean_checks_still_work(self):
        self.assertEqual(load('"Hallo " + value')("Ada"), "Hallo Ada")
        self.assertEqual(load('value.strip()')("  Ada  "), "Ada")
        function = load('isinstance(value, str) and bool(value.strip())')
        self.assertTrue(function(" Ada "))
        self.assertFalse(function("   "))
        self.assertFalse(function(None))
