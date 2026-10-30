import unittest

from src.obsidian_policy import DecisionCode, ReasonCode
from src.obsidian_policy.core import assess_request


class TestObsidianDecisionCodes(unittest.TestCase):
    def setUp(self):
        self.request = {
            "purpose": "Review my supplied configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }

    def test_approved_request_has_stable_codes(self):
        result = assess_request(self.request)

        self.assertEqual(result["decision"], "approved")
        self.assertEqual(result["decision_code"], DecisionCode.APPROVED.value)
        self.assertEqual(
            result["reason_codes"],
            [ReasonCode.REQUEST_WITHIN_DEFENSIVE_SCOPE.value],
        )

    def test_each_human_reason_has_one_machine_code(self):
        request = {
            "purpose": "",
            "target_scope": "internet",
            "authorization_confirmed": False,
            "capabilities": ["unknown", "unknown"],
            "extra": True,
        }

        result = assess_request(request)

        self.assertEqual(len(result["reasons"]), len(result["reason_codes"]))
        self.assertEqual(result["decision_code"], DecisionCode.NEEDS_CLARIFICATION.value)
        self.assertIn(ReasonCode.FIELD_UNKNOWN.value, result["reason_codes"])
        self.assertIn(ReasonCode.CAPABILITY_DUPLICATE.value, result["reason_codes"])
        self.assertIn(ReasonCode.CAPABILITY_UNKNOWN.value, result["reason_codes"])

    def test_prohibited_and_active_rejections_have_distinct_codes(self):
        cases = (
            ("obtain_credentials", ReasonCode.CAPABILITY_PROHIBITED),
            (
                "run_authorized_lab_scan",
                ReasonCode.CAPABILITY_ACTIVE_CONTRACT_REQUIRED,
            ),
        )
        for capability, reason_code in cases:
            with self.subTest(capability=capability):
                request = dict(self.request, capabilities=[capability])
                result = assess_request(request)
                self.assertEqual(result["decision_code"], DecisionCode.REJECTED.value)
                self.assertEqual(result["reason_codes"], [reason_code.value])

    def test_codes_are_enum_backed_and_not_free_text(self):
        decision_values = {code.value for code in DecisionCode}
        reason_values = {code.value for code in ReasonCode}
        result = assess_request("invalid")

        self.assertIn(result["decision_code"], decision_values)
        self.assertTrue(set(result["reason_codes"]).issubset(reason_values))


if __name__ == "__main__":
    unittest.main()
