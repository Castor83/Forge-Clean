import unittest

from src.forge_runtime.audited_decision_suite import run_audited_decision_gate


VALID_SOURCE = '''from .audit import append_audit_assessment
from .policy import assess_request
from .socket_action import run_socket_action

def assess_audit_then_execute(request, audit_path):
    assessment = assess_request(request)
    append_audit_assessment(audit_path, request, assessment)
    if assessment["decision"] != "approved":
        return assessment, None
    execution = run_socket_action(request)
    return assessment, execution
'''


class TestForgeAuditedDecisionSuite(unittest.TestCase):
    def run_source(self, source):
        return run_audited_decision_gate({"agent/decision_gate.py": source})

    def test_exact_fail_closed_gate_passes(self):
        self.assertIsNone(self.run_source(VALID_SOURCE))

    def test_rejects_result_release_before_audit(self):
        changed = VALID_SOURCE.replace(
            "    append_audit_assessment(audit_path, request, assessment)\n",
            "    if assessment[\"decision\"] != \"approved\":\n"
            "        return assessment, None\n"
            "    append_audit_assessment(audit_path, request, assessment)\n",
            1,
        )
        self.assertIn("exact policy", self.run_source(changed))

    def test_rejects_optional_or_swallowed_audit(self):
        optional = VALID_SOURCE.replace(
            "    append_audit_assessment(audit_path, request, assessment)",
            "    if audit_path:\n"
            "        append_audit_assessment(audit_path, request, assessment)",
        )
        swallowed = VALID_SOURCE.replace(
            "    append_audit_assessment(audit_path, request, assessment)",
            "    try:\n"
            "        append_audit_assessment(audit_path, request, assessment)\n"
            "    except OSError:\n"
            "        pass",
        )
        self.assertIn("exact policy", self.run_source(optional))
        self.assertIn("exact policy", self.run_source(swallowed))

    def test_rejects_socket_before_audit(self):
        changed = VALID_SOURCE.replace(
            "    assessment = assess_request(request)\n"
            "    append_audit_assessment(audit_path, request, assessment)",
            "    assessment = assess_request(request)\n"
            "    execution = run_socket_action(request)\n"
            "    append_audit_assessment(audit_path, request, assessment)",
        )
        self.assertIn("exact policy", self.run_source(changed))


if __name__ == "__main__":
    unittest.main()
