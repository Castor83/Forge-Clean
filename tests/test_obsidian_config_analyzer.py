import copy
import unittest
from unittest.mock import patch

from src.obsidian_policy.config_analyzer import analyze_application_config


class TestObsidianConfigAnalyzer(unittest.TestCase):
    def setUp(self):
        self.safe_config = {
            "debug": False,
            "tls_enabled": True,
            "logging_enabled": True,
            "allowed_hosts": ["app.test"],
            "admin_interface_public": False,
        }

    def test_safe_config_has_no_findings(self):
        result = analyze_application_config(self.safe_config)
        self.assertEqual(result["decision"], "completed")
        self.assertEqual(result["findings"], [])

    def test_reports_each_fixed_insecure_setting_in_stable_order(self):
        config = {
            "debug": True,
            "tls_enabled": False,
            "logging_enabled": False,
            "allowed_hosts": ["*"],
            "admin_interface_public": True,
        }
        result = analyze_application_config(config)
        self.assertEqual(
            [finding["rule_id"] for finding in result["findings"]],
            [
                "debug_enabled",
                "tls_disabled",
                "security_logging_disabled",
                "wildcard_allowed_host",
                "public_admin_interface",
            ],
        )

    def test_requires_exact_schema(self):
        for field in self.safe_config:
            with self.subTest(missing=field):
                config = dict(self.safe_config)
                del config[field]
                result = analyze_application_config(config)
                self.assertEqual(result["decision"], "needs_clarification")
                self.assertIn(f"missing field: {field}", result["reasons"])

        config = dict(self.safe_config, extra=True)
        result = analyze_application_config(config)
        self.assertEqual(result["decision"], "needs_clarification")
        self.assertIn("unknown field: extra", result["reasons"])

    def test_rejects_wrong_exact_types(self):
        invalid_values = {
            "debug": 1,
            "tls_enabled": "yes",
            "logging_enabled": None,
            "allowed_hosts": ("app.test",),
            "admin_interface_public": 0,
        }
        for field, value in invalid_values.items():
            with self.subTest(field=field):
                config = dict(self.safe_config, **{field: value})
                self.assertEqual(
                    analyze_application_config(config)["decision"],
                    "needs_clarification",
                )

    def test_enforces_allowed_host_limits(self):
        too_many = dict(self.safe_config, allowed_hosts=["host"] * 21)
        too_long = dict(self.safe_config, allowed_hosts=["a" * 254])
        empty = dict(self.safe_config, allowed_hosts=["  "])
        for config in (too_many, too_long, empty):
            with self.subTest(config=config):
                self.assertEqual(
                    analyze_application_config(config)["decision"],
                    "needs_clarification",
                )

    def test_sensitive_fields_are_not_echoed_with_values(self):
        config = dict(self.safe_config, api_token="do-not-echo")
        result = analyze_application_config(config)
        self.assertEqual(result["decision"], "rejected")
        self.assertNotIn("do-not-echo", repr(result))
        self.assertTrue(any("api_token" in reason for reason in result["reasons"]))

    def test_does_not_mutate_input_and_is_deterministic(self):
        original = copy.deepcopy(self.safe_config)
        first = analyze_application_config(self.safe_config)
        second = analyze_application_config(self.safe_config)
        self.assertEqual(self.safe_config, original)
        self.assertEqual(first, second)

    def test_has_no_runtime_io_calls(self):
        with (
            patch("builtins.open", side_effect=AssertionError("file access")),
            patch("subprocess.run", side_effect=AssertionError("process access")),
            patch("socket.socket", side_effect=AssertionError("network access")),
        ):
            self.assertEqual(
                analyze_application_config(self.safe_config)["decision"], "completed"
            )


if __name__ == "__main__":
    unittest.main()
