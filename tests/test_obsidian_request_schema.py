import copy
import unittest

from src.obsidian_policy.core import assess_request


class TestObsidianRequestSchema(unittest.TestCase):
    def setUp(self):
        self.request = {
            "purpose": "Review my supplied configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }

    def test_exact_schema_is_approved(self):
        self.assertEqual(assess_request(self.request)["decision"], "approved")

    def test_unknown_field_needs_clarification_and_is_not_ignored(self):
        request = dict(self.request, authorization_override=True)

        result = assess_request(request)

        self.assertEqual(result["decision"], "needs_clarification")
        self.assertIn("unknown field: authorization_override", result["reasons"])

    def test_non_string_field_name_needs_clarification_without_echoing_value(self):
        request = dict(self.request)
        request[1] = "secret value"

        result = assess_request(request)

        self.assertEqual(result["decision"], "needs_clarification")
        self.assertIn("all request field names must be strings", result["reasons"])
        self.assertNotIn("secret value", repr(result))

    def test_duplicate_capability_needs_clarification(self):
        request = dict(
            self.request,
            capabilities=[
                "review_user_provided_configuration",
                " review_user_provided_configuration ",
            ],
        )

        result = assess_request(request)

        self.assertEqual(result["decision"], "needs_clarification")
        self.assertIn(
            "duplicate capability: review_user_provided_configuration",
            result["reasons"],
        )

    def test_unvalidated_contract_field_cannot_enable_active_capability(self):
        request = dict(
            self.request,
            capabilities=["run_authorized_lab_scan"],
            execution_contract={
                "contract_type": "authorized_lab_scan_v1",
                "validated": True,
            },
        )

        result = assess_request(request)

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("validated execution contract", result["reasons"][0])

    def test_prohibited_capability_wins_over_schema_errors(self):
        request = dict(
            self.request,
            capabilities=["obtain_credentials"],
            authorization_override=True,
        )

        result = assess_request(request)

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("prohibited capability", result["reasons"][0])

    def test_schema_validation_does_not_mutate_input(self):
        request = dict(self.request, unexpected="value")
        original = copy.deepcopy(request)

        assess_request(request)

        self.assertEqual(request, original)


if __name__ == "__main__":
    unittest.main()
