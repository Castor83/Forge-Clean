import unittest

from src.forge_runtime.contracts import SubagentResult
from src.forge_runtime.result_parser import parse_subagent_result


class TestParseSubagentResult(unittest.TestCase):
    def test_valid_result(self):
        result = parse_subagent_result(
            "builder",
            '{"role":"builder","passed":true,"summary":"Built",'
            '"findings":["Created file","Added tests"]}\n',
        )

        self.assertEqual(
            result,
            SubagentResult(
                role="builder",
                passed=True,
                summary="Built",
                findings=("Created file", "Added tests"),
            ),
        )

    def test_all_roles_and_false_result(self):
        for role in ("builder", "tester", "reviewer"):
            with self.subTest(role=role):
                result = parse_subagent_result(
                    role,
                    '{"role":"' + role + '","passed":false,'
                    '"summary":"Not approved","findings":[]}',
                )
                self.assertEqual(result.role, role)
                self.assertFalse(result.passed)

    def test_rejects_invalid_expected_role_and_subclass(self):
        class StringSubclass(str):
            pass

        for role in ("invalid", StringSubclass("builder"), 1):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    parse_subagent_result(role, "{}")

    def test_rejects_non_string_response_and_subclass(self):
        class StringSubclass(str):
            pass

        for response in (None, {}, StringSubclass("{}")):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    parse_subagent_result("builder", response)

    def test_rejects_invalid_json_and_non_object(self):
        for response in ("not json", "[]", '"text"', "null"):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    parse_subagent_result("builder", response)

    def test_rejects_missing_and_extra_fields(self):
        responses = (
            '{"role":"builder","passed":true,"summary":"Built"}',
            '{"role":"builder","passed":true,"summary":"Built",'
            '"findings":[],"extra":1}',
        )
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    parse_subagent_result("builder", response)

    def test_rejects_role_mismatch(self):
        with self.assertRaises(ValueError):
            parse_subagent_result(
                "tester",
                '{"role":"builder","passed":true,"summary":"Built",'
                '"findings":[]}',
            )

    def test_rejects_invalid_passed_type(self):
        for passed in ("true", 1, None):
            response = (
                '{"role":"builder","passed":'
                + (f'"{passed}"' if isinstance(passed, str) else str(passed).lower())
                + ',"summary":"Built","findings":[]}'
            )
            with self.subTest(passed=passed):
                with self.assertRaises(ValueError):
                    parse_subagent_result("builder", response)

    def test_rejects_empty_or_invalid_summary(self):
        for summary in ("", "   ", 1):
            encoded = f'"{summary}"' if isinstance(summary, str) else str(summary)
            response = (
                '{"role":"builder","passed":true,"summary":'
                + encoded
                + ',"findings":[]}'
            )
            with self.subTest(summary=summary):
                with self.assertRaises(ValueError):
                    parse_subagent_result("builder", response)

    def test_rejects_invalid_findings(self):
        responses = (
            '{"role":"builder","passed":true,"summary":"Built","findings":{}}',
            '{"role":"builder","passed":true,"summary":"Built","findings":[""]}',
            '{"role":"builder","passed":true,"summary":"Built","findings":["   "]}',
            '{"role":"builder","passed":true,"summary":"Built","findings":[1]}',
        )
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    parse_subagent_result("builder", response)


if __name__ == "__main__":
    unittest.main()
