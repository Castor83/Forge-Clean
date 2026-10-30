import ast
import unittest
from unittest.mock import patch

from src.forge_runtime.task_runner import _load_restricted_function


class ResourceCounterAllowlistTests(unittest.TestCase):
    def test_new_expression_types_cannot_bypass_resource_accounting(self):
        # Simulate a future extension of the separate execution allowlist.
        # The resource checker must still deny syntax it has no cost rule for.
        for expression in ("value * 2", "[value]", "value if value else ''"):
            tree = ast.parse("def probe(value):\n    return " + expression + "\n")
            with self.subTest(expression=expression), \
                    patch("src.forge_runtime.task_runner._validate_restricted_expression"), \
                    patch("src.forge_runtime.task_runner.compile", wraps=compile) as compiler, \
                    patch("src.forge_runtime.task_runner.exec", wraps=exec) as execute:
                with self.assertRaisesRegex(ValueError, "unsupported resource node"):
                    _load_restricted_function(tree, "probe")
                compiler.assert_not_called()
                execute.assert_not_called()

    def test_new_name_cannot_be_free_when_execution_allowlist_is_extended(self):
        tree = ast.parse("def probe(value):\n    return future_helper\n")
        with patch("src.forge_runtime.task_runner._validate_restricted_expression"), \
                patch("src.forge_runtime.task_runner.compile", wraps=compile) as compiler:
            with self.assertRaisesRegex(ValueError, "unknown resource name"):
                _load_restricted_function(tree, "probe")
        compiler.assert_not_called()

    def test_supported_expression_combinations_keep_their_real_results(self):
        examples = (
            ('"Hi " + value', "Ada", "Hi Ada"),
            ('f"Hi {value.strip()}"', " Ada ", "Hi Ada"),
            ('isinstance(value, str) and bool(value.strip())', " Ada ", True),
            ('(not value or value == "x") and value is not None', "", True),
            ('value != "x" and value not in "xyz"', "q", True),
            ('value is None or value in "xyz"', None, True),
            ('f"{bool(value)}:{value + value}"', 3, "True:6"),
        )
        for expression, value, expected in examples:
            with self.subTest(expression=expression):
                tree = ast.parse("def probe(value):\n    return " + expression + "\n")
                function = _load_restricted_function(tree, "probe")
                self.assertEqual(function(value), expected)

    def test_parameter_shadowing_keeps_value_cost_instead_of_builtin_cost(self):
        tree = ast.parse('def probe(str):\n    return f"{str}{str}"\n')
        function = _load_restricted_function(tree, "probe")
        self.assertEqual(function("Ada"), "AdaAda")
        with self.assertRaisesRegex(ValueError, "value resource limit"):
            function("x" * 32769)
