import ast
import unittest
from unittest.mock import patch

from src.forge_runtime.task_runner import _load_restricted_function


class ReviewResourceOrderTests(unittest.TestCase):
    def test_formatted_method_growth_is_rejected_before_compile_or_exec(self):
        source = 'def probe(value):\n    return f"' + '{value.strip}' * 1200 + '"\n'
        tree = ast.parse(source)
        with patch("src.forge_runtime.task_runner.compile", wraps=compile) as compiler, \
                patch("src.forge_runtime.task_runner.exec", wraps=exec) as execute:
            with self.assertRaisesRegex(ValueError, "value resource limit"):
                _load_restricted_function(tree, "probe")
        compiler.assert_not_called()
        execute.assert_not_called()

    def test_input_rejection_happens_before_any_generated_body_line(self):
        tree = ast.parse("def probe(value):\n    return value + value\n")
        function = _load_restricted_function(tree, "probe")
        calls = []

        def trace(frame, event, arg):
            if frame.f_code.co_filename == "<restricted-forge-check>":
                calls.append(event)
            return trace

        import sys
        old_trace = sys.gettrace()
        sys.settrace(trace)
        try:
            with self.assertRaisesRegex(ValueError, "value resource limit"):
                function("x" * 32769)
            self.assertEqual(calls, [])
            self.assertEqual(function("x"), "xx")
            self.assertIn("call", calls)
            self.assertIn("line", calls)
        finally:
            sys.settrace(old_trace)

    def test_small_method_formatting_still_has_its_existing_semantics(self):
        tree = ast.parse('def probe(value):\n    return f"{value.strip}"\n')
        result = _load_restricted_function(tree, "probe")("")
        self.assertIn("strip of str object", result)
        self.assertLess(len(result), 128)

    def test_integer_input_limit_does_not_claim_an_intermediate_result_limit(self):
        tree = ast.parse("def probe(value):\n    return value + value\n")
        result = _load_restricted_function(tree, "probe")(1 << 4095)
        self.assertEqual(result.bit_length(), 4097)
