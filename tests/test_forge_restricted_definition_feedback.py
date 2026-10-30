import ast
import unittest
from unittest.mock import patch

from src.forge_runtime import task_runner as runner


BODY_MESSAGE = (
    'restricted function body must consist exclusively of one return statement; '
    'one leading string docstring is permitted; assignments and other '
    'statements are not permitted')
MODULE_MESSAGE = (
    'restricted check requires exactly one function definition; '
    'all other module-level statements must be string expressions')
DEFINITION_MESSAGE = (
    'restricted function uses unsupported signature or definition features; '
    'decorators, type parameters, return annotations, type comments, '
    'positional-only parameters, variadic parameters, keyword-only parameters '
    'and default arguments are not permitted')
PARAMETER_CALL_MESSAGE = (
    'restricted function calls through function parameters are not permitted; '
    'this also applies when the called parameter has the same name as a built-in')


class TestRestrictedDefinitionFeedback(unittest.TestCase):
    def reject(self, source, message):
        tree = ast.parse(source)
        with patch.object(runner, 'compile', create=True) as compiler, \
                patch.object(runner, 'exec', create=True) as execution:
            with self.assertRaises(ValueError) as caught:
                runner._load_restricted_function(tree, 'probe')
            self.assertEqual(str(caught.exception), message)
            compiler.assert_not_called()
            execution.assert_not_called()

    def test_body_message_covers_extra_statements_and_docstring_boundary(self):
        bodies = (
            'value = True\n    return value',
            'pass\n    return True',
            'return True\n    return False',
            '"first docstring"\n    "second string"\n    return True',
            '"only docstring"',
        )
        for body in bodies:
            with self.subTest(body=body):
                self.reject('def probe():\n    ' + body + '\n', BODY_MESSAGE)

    def test_optional_leading_function_docstring_is_still_accepted(self):
        for body in ('return True', '"description"\n    return True'):
            with self.subTest(body=body):
                function = runner._load_restricted_function(
                    ast.parse('def probe():\n    ' + body + '\n'), 'probe')
                self.assertIs(function(), True)

    def test_module_message_covers_count_and_other_statement_types(self):
        for source in (
            'extra = 1\ndef probe():\n    return True\n',
            'import math\ndef probe():\n    return True\n',
            'def probe():\n    return True\ndef other():\n    return True\n',
            '"text without a function"\n',
        ):
            with self.subTest(source=source):
                self.reject(source, MODULE_MESSAGE)

    def test_multiple_module_strings_at_other_positions_remain_accepted(self):
        source = ('"first"\n"second"\ndef probe():\n'
                  '    return True\n"after function"\n')
        self.assertIs(runner._load_restricted_function(ast.parse(source), 'probe')(), True)

    def test_definition_message_includes_decorators_and_signature_features(self):
        for source in (
            '@decorator\ndef probe():\n    return True\n',
            'def probe(value=True):\n    return True\n',
            'def probe(value, /):\n    return True\n',
            'def probe(*args):\n    return True\n',
            'def probe(**kwargs):\n    return True\n',
            'def probe(*, value):\n    return True\n',
            'def probe() -> bool:\n    return True\n',
            'def probe[T]():\n    return True\n',
        ):
            with self.subTest(source=source):
                self.reject(source, DEFINITION_MESSAGE)

    def test_calls_through_any_parameter_are_rejected_without_shadow_claim(self):
        for name in ('value', 'bool', 'str', 'isinstance'):
            with self.subTest(name=name):
                self.reject(f'def probe({name}):\n    return {name}()\n',
                            PARAMETER_CALL_MESSAGE)


if __name__ == '__main__':
    unittest.main()
