import ast
import unittest
from unittest.mock import patch

from src.forge_runtime.task_runner import (
    FunctionCase, StaticCheck, _load_restricted_function, _run_restricted_function_cases)


class RestrictedBuiltinBindingTests(unittest.TestCase):
    def test_function_cannot_replace_a_permitted_builtin_before_code_execution(self):
        sources = {
            "bool": "def bool(value):\n    return bool(value)\n",
            "isinstance": "def isinstance(value, kind):\n    return isinstance(value, str)\n",
            "str": "def str(value):\n    return isinstance(value, str)\n"}
        for name, source in sources.items():
            tree = ast.parse(source)
            with self.subTest(name=name), patch("src.forge_runtime.task_runner.exec", create=True) as execute:
                with self.assertRaisesRegex(ValueError, "built-in"):
                    _load_restricted_function(tree, name)
                execute.assert_not_called()

    def test_parameters_cannot_replace_helpers_used_by_an_allowed_call(self):
        sources = (
            "def check(bool):\n    return bool(bool)\n",
            "def check(isinstance):\n    return isinstance(isinstance, str)\n",
            "def check(str):\n    return isinstance(str, str)\n")
        for source in sources:
            tree = ast.parse(source)
            with self.subTest(source=source), patch("src.forge_runtime.task_runner.exec", create=True) as execute:
                with self.assertRaisesRegex(ValueError, "built-in"):
                    _load_restricted_function(tree, "check")
                execute.assert_not_called()

    def test_normal_helpers_and_parameters_not_used_as_helpers_still_work(self):
        check = StaticCheck("python_function_cases", "agent/check.py", function="check",
            cases=(FunctionCase(("x",), True), FunctionCase(("   ",), False),
                   FunctionCase((None,), False)))
        self.assertIsNone(_run_restricted_function_cases(
            "def check(value):\n    return isinstance(value, str) and bool(value.strip())\n", check))
        identity = StaticCheck("python_function_cases", "agent/check.py", function="check",
            cases=(FunctionCase((True,), True), FunctionCase((False,), False)))
        self.assertIsNone(_run_restricted_function_cases("def check(bool):\n    return bool\n", identity))
