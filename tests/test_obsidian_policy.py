import copy
import unittest
from unittest.mock import patch

from src.obsidian_policy.core import assess_request


class TestObsidianPolicy(unittest.TestCase):
    def setUp(self):
        self.safe_request = {
            "purpose": "Explain the hardening of my local test configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }

    def test_approves_complete_defensive_request(self):
        self.assertEqual(assess_request(self.safe_request)["decision"], "approved")

    def test_requires_each_mandatory_field(self):
        expected_reason_fragments = {
            "purpose": "purpose",
            "target_scope": "target_scope",
            "authorization_confirmed": "authorization_confirmed",
            "capabilities": "capabilities",
        }
        for field, fragment in expected_reason_fragments.items():
            with self.subTest(field=field):
                request = dict(self.safe_request)
                del request[field]
                result = assess_request(request)
                self.assertEqual(result["decision"], "needs_clarification")
                self.assertTrue(any(fragment in reason for reason in result["reasons"]))

    def test_does_not_approve_unrecognized_target_scope(self):
        request = dict(self.safe_request, target_scope="internet_host")
        self.assertEqual(assess_request(request)["decision"], "needs_clarification")

    def test_rejects_every_prohibited_capability(self):
        prohibited = [
            "establish_persistence",
            "evade_detection",
            "obtain_credentials",
            "exfiltrate_data",
            "disrupt_system",
            "target_unauthorized_system",
            "execute_generated_code",
        ]
        for capability in prohibited:
            with self.subTest(capability=capability):
                request = dict(self.safe_request, capabilities=[capability])
                result = assess_request(request)
                self.assertEqual(result["decision"], "rejected")
                self.assertIn(capability, result["reasons"][0])

    def test_prohibited_capability_wins_over_missing_fields(self):
        result = assess_request({"capabilities": ["obtain_credentials"]})
        self.assertEqual(result["decision"], "rejected")

    def test_unknown_capability_needs_clarification(self):
        request = dict(self.safe_request, capabilities=["scan_everything"])
        result = assess_request(request)
        self.assertEqual(result["decision"], "needs_clarification")
        self.assertIn("unknown capability: scan_everything", result["reasons"])

    def test_invalid_request_type_needs_clarification(self):
        result = assess_request("not structured")
        self.assertEqual(result["decision"], "needs_clarification")

    def test_does_not_mutate_input(self):
        original = copy.deepcopy(self.safe_request)
        assess_request(self.safe_request)
        self.assertEqual(self.safe_request, original)

    def test_is_deterministic(self):
        self.assertEqual(assess_request(self.safe_request), assess_request(self.safe_request))

    def test_has_no_runtime_io_calls(self):
        with (
            patch("builtins.open", side_effect=AssertionError("file access")),
            patch("subprocess.run", side_effect=AssertionError("process access")),
            patch("socket.socket", side_effect=AssertionError("network access")),
        ):
            self.assertEqual(assess_request(self.safe_request)["decision"], "approved")


if __name__ == "__main__":
    unittest.main()
