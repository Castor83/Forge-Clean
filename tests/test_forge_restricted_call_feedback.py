import unittest
from unittest.mock import patch

from src.forge_runtime import task_runner as runner


class TestRestrictedCallFeedback(unittest.TestCase):
    def check(self, expression, args=(' temporary ',), expected=True):
        check = runner.StaticCheck(
            'python_function_cases', 'agent/probe.py', function='probe',
            cases=(runner.FunctionCase(args, expected),))
        return runner._run_restricted_function_cases(
            'def probe(value):\n    return ' + expression + '\n', check)

    def test_call_result_strip_is_rejected_before_execution_with_rule(self):
        with patch.object(runner, '_check_restricted_value_budget') as execution:
            failure = self.check('str(value).strip() == "temporary"')
        execution.assert_not_called()
        self.assertIn('unsafe method call', failure)
        self.assertIn('line 2', failure)
        self.assertIn("method 'strip' on Call", failure)
        self.assertIn('only strip() directly on a function parameter', failure)
        self.assertIn('without arguments', failure)

    def test_standalone_str_reports_type_name_rule(self):
        failure = self.check('str(value) == "temporary"')
        self.assertEqual(
            failure,
            'restricted function contains an unsafe call: '
            'line 2, str(...) is not permitted; '
            'use of str as the type name in isinstance(..., str) is permitted')

    def test_existing_type_name_expressions_are_not_newly_restricted(self):
        # Record observed compatibility, not a new intended-use decision.
        for expression in ('bool(str)', 'str == str'):
            with self.subTest(expression=expression):
                self.assertIsNone(self.check(expression))

    def test_permitted_forms_still_execute_and_match_values(self):
        for value, expected in [(' temporary ', True), ('permanent', False),
                                (None, False), (0, False)]:
            with self.subTest(value=value):
                self.assertIsNone(self.check(
                    'isinstance(value, str) and value.strip() == "temporary"',
                    (value,), expected))

    def test_other_method_receivers_and_arguments_remain_rejected(self):
        for expression in ['value.lower()', 'value.strip(" ")',
                           '"temporary".strip()', 'bool(value).strip()']:
            with self.subTest(expression=expression):
                self.assertIn('unsafe method call', self.check(expression))


if __name__ == '__main__':
    unittest.main()
