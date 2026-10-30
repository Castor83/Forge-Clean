import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_protocol import discuss, parse_proposal_task
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_protocol import answer, proposal


class ConversationFeasibilityTests(unittest.TestCase):
    def test_python_keyword_cannot_be_the_function_name(self):
        for name in ("class", "return", "None", "True", "async"):
            value = proposal()
            value["checks"][0]["function"] = name
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "reserviert"):
                parse_proposal_task(value)

    def test_fixed_function_cannot_have_different_case_argument_counts(self):
        value = proposal()
        value["checks"][0]["cases"][1]["args"] = []
        with self.assertRaisesRegex(ValueError, "Argumentanzahl"):
            parse_proposal_task(value)

    def test_one_input_cannot_require_conflicting_outputs(self):
        value = proposal()
        contradictory = copy.deepcopy(value["checks"][0]["cases"][0])
        contradictory["expected"] = "Andere Ausgabe"
        value["checks"][0]["cases"].append(contradictory)
        with self.assertRaisesRegex(ValueError, "widersprüchlich"):
            parse_proposal_task(value)

    def test_conflicting_output_types_are_rejected_even_when_equal(self):
        value = proposal()
        value["checks"][0]["cases"][0]["expected"] = 1
        contradictory = copy.deepcopy(value["checks"][0]["cases"][0])
        contradictory["expected"] = True
        value["checks"][0]["cases"].append(contradictory)
        with self.assertRaisesRegex(ValueError, "widersprüchlich"):
            parse_proposal_task(value)

    def test_valid_soft_keyword_name_and_distinct_scalar_types_remain_supported(self):
        value = proposal()
        value["checks"][0]["function"] = "match"
        value["checks"][0]["cases"] = [
            {"args": [1], "expected": "integer"},
            {"args": [1.0], "expected": "float"},
            {"args": [True], "expected": "boolean"}]
        spec = parse_proposal_task(value)
        self.assertEqual(len(spec.checks[0].cases), 3)

    def test_impossible_model_proposal_gets_bounded_correction_before_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                invalid = proposal()
                invalid["checks"][0]["function"] = "class"
                seen = []
                responses = iter([answer(invalid), answer(proposal())])
                def chat(messages):
                    seen.append(messages)
                    return next(responses)
                discuss(task, "Begrüße einen Namen", chat)
                self.assertEqual(len(seen), 2)
                feedback = json.loads(seen[1][-1][1])
                self.assertIn("reserviert", feedback["error"])
                self.assertEqual(task.data["proposal"], proposal())
                self.assertEqual(task.data["state"], "proposed")
                self.assertFalse(task.is_confirmed())
