import ast
import unittest
from unittest.mock import patch

from src.forge_runtime.task_runner import (
    FunctionCase, StaticCheck, _load_restricted_function, _run_restricted_function_cases)


class RestrictedGenericSignatureTests(unittest.TestCase):
    def test_type_parameters_are_rejected_before_any_code_execution(self):
        sources = (
            "def check[T](value):\n    return value\n",
            "def check[T: bool('x')](value):\n    return value\n",
            "def check[*Values](value):\n    return value\n",
            "def check[**Parameters](value):\n    return value\n")
        for source in sources:
            tree = ast.parse(source)
            with self.subTest(source=source), patch("src.forge_runtime.task_runner.exec", create=True) as execute:
                with self.assertRaisesRegex(ValueError, "unsupported signature"):
                    _load_restricted_function(tree, "check")
                execute.assert_not_called()

    def test_type_parameter_cannot_redirect_bool_call_in_actual_host_check(self):
        check = StaticCheck("python_function_cases", "agent/check.py", function="check",
            cases=(FunctionCase(("x",), True), FunctionCase(("",), False)))
        failure = _run_restricted_function_cases(
            "def check[bool](value):\n    return bool(value)\n", check)
        self.assertIn("unsupported signature", failure)
        self.assertIsNone(_run_restricted_function_cases(
            "def check(value):\n    return bool(value)\n", check))
