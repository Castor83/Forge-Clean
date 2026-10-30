import unittest
from unittest.mock import patch

from src.forge_runtime.audit_acl_contract import EXPECTED_SOURCE as ACL_SOURCE
from src.forge_runtime.durable_audit_contract import EXPECTED_SOURCE
from src.forge_runtime.audit_restart_verifier import verify_durable_audit_restart


class TestForgeAuditRestart(unittest.TestCase):
    def test_fresh_process_observes_persisted_block_and_resolves_without_duplicate(self):
        self.assertIsNone(verify_durable_audit_restart({
            "agent/audit.py": EXPECTED_SOURCE,
            "agent/audit_acl.py": ACL_SOURCE,
        }))

    def test_unreviewed_code_is_rejected_before_child_process(self):
        with patch("src.forge_runtime.audit_restart_verifier.subprocess.run") as run:
            failure = verify_durable_audit_restart({
                "agent/audit.py": EXPECTED_SOURCE + "\nraise RuntimeError('unexpected')\n",
                "agent/audit_acl.py": ACL_SOURCE,
            })
            self.assertIn("exact frozen", failure)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
