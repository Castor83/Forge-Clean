import copy
import unittest
from datetime import datetime, timezone

from src.obsidian_policy import (
    ContractValidationError,
    ValidatedExecutionContract,
    contract_is_current,
    validate_execution_contract,
)
from src.obsidian_policy.core import assess_request


class TestObsidianExecutionContract(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
        self.raw = {
            "contract_id": "12345678-1234-5678-9234-567812345678",
            "contract_type": "authorized_lab_scan_v1",
            "capability": "run_authorized_lab_scan",
            "target_scope": "isolated_local_lab",
            "target": {
                "kind": "ip_literal",
                "address": "127.0.0.1",
                "ports": [8080, 443],
                "transport": "tcp",
            },
            "validity": {
                "not_before_utc": "2026-09-26T11:59:00Z",
                "expires_at_utc": "2026-09-26T12:05:00Z",
            },
            "limits": {
                "max_connections": 8,
                "max_concurrency": 2,
                "max_runtime_seconds": 20,
                "max_bytes_sent": 0,
            },
            "allowed_actions": ["tcp_connect_probe"],
            "approval_points": ["before_execution"],
            "abort_conditions": [
                "authorization_revoked",
                "deadline_reached",
                "host_policy_denied",
                "resource_limit_reached",
                "target_mismatch",
            ],
        }
        self.request = {
            "purpose": "Probe my loopback lab service",
            "target_scope": "isolated_local_lab",
            "authorization_confirmed": True,
            "capabilities": ["run_authorized_lab_scan"],
        }

    def validated_contract(self):
        return validate_execution_contract(
            self.raw, bound_request=self.request, now_utc=self.now
        )

    def test_host_validator_creates_immutable_canonical_contract(self):
        original = copy.deepcopy(self.raw)

        contract = self.validated_contract()

        self.assertEqual(self.raw, original)
        self.assertEqual(contract.target.address, "127.0.0.1")
        self.assertEqual(contract.target.ports, (443, 8080))
        with self.assertRaises((AttributeError, TypeError)):
            contract.target.address = "127.0.0.2"

    def test_direct_construction_cannot_claim_host_validation(self):
        contract = self.validated_contract()
        values = {
            field: getattr(contract, field)
            for field in contract.__dataclass_fields__
            if field != "_seal"
        }

        with self.assertRaisesRegex(ContractValidationError, "host validator"):
            ValidatedExecutionContract(**values, _seal=object())

    def test_raw_or_model_claimed_contract_never_enables_active_request(self):
        request = dict(self.request, execution_contract=dict(self.raw, validated=True))

        result = assess_request(request)

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(
            result["reason_codes"], ["CAPABILITY_ACTIVE_CONTRACT_REQUIRED"]
        )

    def test_valid_contract_is_bound_and_approved_for_fixed_host_execution(self):
        contract = self.validated_contract()

        result = assess_request(
            self.request, validated_contract=contract, now_utc=self.now
        )

        self.assertEqual(result["decision"], "approved")
        self.assertEqual(
            result["reason_codes"], ["CAPABILITY_ACTIVE_CONTRACT_VALIDATED"]
        )

    def test_request_change_invalidates_a_valid_out_of_band_contract(self):
        contract = self.validated_contract()
        request = dict(self.request, model_claimed_approval=True)

        result = assess_request(request, validated_contract=contract, now_utc=self.now)

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(
            result["reason_codes"], ["CAPABILITY_ACTIVE_CONTRACT_REQUIRED"]
        )

    def test_contract_cannot_cross_capability_scope_or_validity_window(self):
        contract = self.validated_contract()
        wrong_scope = dict(self.request, target_scope="owned_system")
        changed_purpose = dict(self.request, purpose="Probe a different service")
        expired_time = datetime(2026, 9, 26, 12, 6, 0, tzinfo=timezone.utc)

        for request, when in (
            (wrong_scope, self.now),
            (changed_purpose, self.now),
            (self.request, expired_time),
        ):
            with self.subTest(request=request, when=when):
                result = assess_request(
                    request, validated_contract=contract, now_utc=when
                )
                self.assertEqual(
                    result["reason_codes"], ["CAPABILITY_ACTIVE_CONTRACT_REQUIRED"]
                )

    def test_non_loopback_hostname_and_oversized_port_set_are_rejected(self):
        cases = []
        hostname = copy.deepcopy(self.raw)
        hostname["target"]["address"] = "localhost"
        cases.append(hostname)
        remote = copy.deepcopy(self.raw)
        remote["target"]["address"] = "192.0.2.1"
        cases.append(remote)
        too_many_ports = copy.deepcopy(self.raw)
        too_many_ports["target"]["ports"] = list(range(1, 18))
        cases.append(too_many_ports)

        for raw in cases:
            with self.subTest(target=raw["target"]):
                with self.assertRaises(ContractValidationError):
                    validate_execution_contract(
                        raw, bound_request=self.request, now_utc=self.now
                    )

    def test_unknown_nested_fields_and_weakened_guards_fail_closed(self):
        cases = []
        unknown = copy.deepcopy(self.raw)
        unknown["target"]["resolve_dns"] = True
        cases.append(unknown)
        sends_data = copy.deepcopy(self.raw)
        sends_data["limits"]["max_bytes_sent"] = 1
        cases.append(sends_data)
        missing_abort = copy.deepcopy(self.raw)
        missing_abort["abort_conditions"].remove("authorization_revoked")
        cases.append(missing_abort)

        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ContractValidationError):
                    validate_execution_contract(
                        raw, bound_request=self.request, now_utc=self.now
                    )

    def test_contract_validity_is_rechecked(self):
        contract = self.validated_contract()

        self.assertTrue(contract_is_current(contract, now_utc=self.now))
        self.assertFalse(
            contract_is_current(
                contract,
                now_utc=datetime(2026, 9, 26, 12, 5, 0, tzinfo=timezone.utc),
            )
        )

    def test_contract_creation_rejects_a_mismatched_bound_request(self):
        for request in (
            dict(self.request, authorization_confirmed=False),
            dict(self.request, capabilities=["explain_security_concepts"]),
            dict(self.request, extra=True),
        ):
            with self.subTest(request=request):
                with self.assertRaises(ContractValidationError):
                    validate_execution_contract(
                        self.raw, bound_request=request, now_utc=self.now
                    )


if __name__ == "__main__":
    unittest.main()
