import copy
import unittest

from src.obsidian_policy.config_analyzer import analyze_application_config


class TestObsidianRedactionContract(unittest.TestCase):
    def setUp(self):
        self.config = {
            "debug": False,
            "tls_enabled": True,
            "logging_enabled": True,
            "allowed_hosts": ["app.test"],
            "admin_interface_public": False,
        }

    def test_known_sensitive_fields_accept_only_exact_marker(self):
        for field in ("api_token", "database_password", "private_key"):
            with self.subTest(field=field):
                config = dict(self.config, **{field: "REDACTED"})
                result = analyze_application_config(config)
                self.assertEqual(result["decision"], "completed")

    def test_every_marker_deviation_rejects_entire_dataset_without_value_echo(self):
        values = ("redacted", " REDACTED", "REDACTED ", "", None, 1, "secret-123")
        for value in values:
            with self.subTest(value=value):
                config = dict(self.config, api_token=value)
                original = copy.deepcopy(config)

                result = analyze_application_config(config)

                self.assertEqual(result["decision"], "rejected")
                self.assertEqual(result["findings"], [])
                self.assertIn("api_token", result["reasons"][0])
                if isinstance(value, str) and value:
                    self.assertNotIn(repr(value), repr(result))
                self.assertEqual(config, original)

    def test_rejection_does_not_analyze_other_insecure_settings(self):
        config = dict(
            self.config,
            debug=True,
            tls_enabled=False,
            api_token="actual-secret",
        )

        result = analyze_application_config(config)

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["findings"], [])
        self.assertNotIn("actual-secret", repr(result))

    def test_unknown_secret_like_field_is_not_silently_accepted(self):
        config = dict(self.config, service_secret="REDACTED")

        result = analyze_application_config(config)

        self.assertEqual(result["decision"], "needs_clarification")
        self.assertTrue(any("service_secret" in reason for reason in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
