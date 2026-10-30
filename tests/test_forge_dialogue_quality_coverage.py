"""Coverage may use any matching text, while every scalar output stays exact."""

import copy
import json
import unittest

from scripts import verify_dialogue_quality as quality
from src.forge_runtime.conversation_protocol import parse_proposal_task
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from tests.test_forge_dialogue_quality_acceptance import proposed


def validator(values=("", "   ", "Hallo", " Welt ")):
    response = proposed("contextual_validator")
    response["task"]["checks"][0]["cases"] = [
        {"args": [value], "expected": bool(value.strip())} for value in values]
    return response


class DialogueQualityCoverageTests(unittest.TestCase):
    def test_alternate_visible_words_cover_the_same_user_requirement(self):
        result = quality.score_response("contextual_validator", validator())
        self.assertEqual(result["required_coverage_categories"], list(quality.VALIDATOR_CATEGORIES))
        self.assertEqual([case["args"] for case in result["required_cases"]],
                         [[""], ["   "], ["Hallo"], [" Welt "]])
        self.assertEqual([case["expected"] for case in result["required_cases"]], [False, False, True, True])

    def test_other_whitespace_and_multiple_valid_extra_cases_are_checked_and_accepted(self):
        values = ("", "\t ", "\n", "Hallo", "Welt", " Welt ", "Hallo ")
        result = quality.score_response("contextual_validator", validator(values))
        self.assertEqual(len(result["required_cases"]), 4)

    def test_omitting_any_one_coverage_category_is_rejected(self):
        values = ("", "   ", "Hallo", " Welt ")
        for missing in range(len(values)):
            remaining = values[:missing] + values[missing + 1:]
            with self.subTest(missing=quality.VALIDATOR_CATEGORIES[missing]), self.assertRaises(ValueError):
                quality.score_response("contextual_validator", validator(remaining))

    def test_duplicate_case_cannot_replace_missing_category(self):
        response = validator(("", "   ", "Hallo"))
        response["task"]["checks"][0]["cases"].append(
            copy.deepcopy(response["task"]["checks"][0]["cases"][0]))
        with self.assertRaises(ValueError):
            quality.score_response("contextual_validator", response)

    def test_each_wrong_output_is_rejected_for_all_four_categories(self):
        for index in range(4):
            response = validator()
            case = response["task"]["checks"][0]["cases"][index]
            case["expected"] = not case["expected"]
            with self.subTest(index=index), self.assertRaises(ValueError):
                quality.score_response("contextual_validator", response)

    def test_integer_lookalikes_are_not_boolean_results(self):
        for index in range(4):
            response = validator()
            case = response["task"]["checks"][0]["cases"][index]
            case["expected"] = int(case["expected"])
            with self.subTest(index=index), self.assertRaises(ValueError):
                quality.score_response("contextual_validator", response)

    def test_wrong_extra_output_is_rejected_even_with_complete_coverage(self):
        response = validator()
        response["task"]["checks"][0]["cases"].append({"args": ["Zusatz"], "expected": False})
        with self.assertRaises(ValueError):
            quality.score_response("contextual_validator", response)

    def test_zero_arguments_remain_rejected_for_same_and_different_outputs(self):
        for outputs in ((True, True), (True, False)):
            response = validator()
            response["task"]["checks"][0]["cases"] = [
                {"args": [], "expected": output} for output in outputs]
            with self.subTest(outputs=outputs), self.assertRaises(ValueError):
                parse_proposal_task(response["task"])
            with self.subTest(outputs=outputs), self.assertRaises(ValueError):
                quality.score_response("contextual_validator", response)

    def test_valid_nonempty_argument_lists_preserve_the_existing_host_contract(self):
        response = validator()
        spec = parse_proposal_task(response["task"])
        self.assertEqual([case.args for case in spec.checks[0].cases],
                         [("",), ("   ",), ("Hallo",), (" Welt ",)])

    def test_actual_model_schema_requires_nonempty_argument_list(self):
        observed = []
        content = json.dumps(validator(), ensure_ascii=False)
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        def opener(request, timeout):
            observed.append(json.loads(request.data))
            return Response()
        client = LMStudioChatClient("forge-30b", opener=opener, response_kind="conversation")
        self.assertEqual(client((("system", "rules"), ("user", "validator"))), content)
        task_schema = observed[0]["response_format"]["json_schema"]["schema"]["anyOf"][1]["properties"]["task"]
        args = task_schema["properties"]["checks"]["items"]["properties"]["cases"]["items"]["properties"]["args"]
        self.assertEqual(args["minItems"], 1)
        self.assertEqual(args["maxItems"], 4)


if __name__ == "__main__":
    unittest.main()
