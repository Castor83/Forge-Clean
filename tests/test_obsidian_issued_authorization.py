"""Behavior proofs for host-issued contracts and one-use socket grants."""

from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import gc
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from weakref import ref

from src.obsidian_policy import (
    ContractValidationError,
    ExecutionGuard,
    ValidatedExecutionContract,
    contract_is_current,
    request_matches_contract,
    run_authorized_lab_scan,
    validate_execution_contract,
)
from src.obsidian_policy import active_scan, execution_contract
from src.obsidian_policy.active_scan import _AuthorizedProbe, _tcp_connect_probe
from src.obsidian_policy.core import assess_request


def _values(template):
    return {item.name: getattr(template, item.name) for item in fields(template)}


def _uninitialized_clone(template, *, cls=None, seal=None, omit_seal=False):
    forged = object.__new__(cls or type(template))
    for name, value in _values(template).items():
        if name == "_seal":
            if omit_seal:
                continue
            value = seal
        object.__setattr__(forged, name, value)
    return forged


class TestIssuedAuthorization(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
        self.request = {
            "purpose": "Probe my loopback lab service",
            "target_scope": "isolated_local_lab",
            "authorization_confirmed": True,
            "capabilities": ["run_authorized_lab_scan"],
        }
        self.raw = {
            "contract_id": "12345678-1234-5678-9234-567812345678",
            "contract_type": "authorized_lab_scan_v1",
            "capability": "run_authorized_lab_scan",
            "target_scope": "isolated_local_lab",
            "target": {
                "kind": "ip_literal",
                "address": "127.0.0.1",
                "ports": [443],
                "transport": "tcp",
            },
            "validity": {
                "not_before_utc": "2026-10-03T11:59:00Z",
                "expires_at_utc": "2026-10-03T12:05:00Z",
            },
            "limits": {
                "max_connections": 2,
                "max_concurrency": 1,
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
        self.contract = self.issue_contract()
        self.addCleanup(patch.stopall)
        patch("src.obsidian_policy.active_scan._utc_now", return_value=self.now).start()
        patch("src.obsidian_policy.active_scan._monotonic", return_value=100.0).start()
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audit_path = Path(self.directory.name) / "audit.jsonl"

    def issue_contract(self):
        return validate_execution_contract(
            self.raw, bound_request=self.request, now_utc=self.now
        )

    def issue_probe(self):
        guard = ExecutionGuard(self.contract)
        status, authorization = guard.reserve_probe(
            address="127.0.0.1", port=443, transport="tcp"
        )
        self.assertIsNone(status)
        self.assertIsNotNone(authorization)
        return authorization

    def socket_connection(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.connect_ex.return_value = 0
        return connection

    def assert_contract_denied(self, candidate):
        self.assertFalse(request_matches_contract(self.request, candidate))
        self.assertFalse(contract_is_current(candidate, now_utc=self.now))
        result = assess_request(
            self.request, validated_contract=candidate, now_utc=self.now
        )
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(
            result["reason_codes"], ["CAPABILITY_ACTIVE_CONTRACT_REQUIRED"]
        )
        with self.assertRaises((TypeError, ValueError)):
            ExecutionGuard(candidate)
        with patch("src.obsidian_policy.active_scan.socket.socket") as factory:
            result = run_authorized_lab_scan(
                self.request, candidate, audit_path=self.audit_path
            )
        self.assertEqual(result.status_code, "EXECUTION_HOST_POLICY_DENIED")
        self.assertEqual(result.attempts, 0)
        factory.assert_not_called()

    def assert_probe_denied(self, candidate):
        with patch("src.obsidian_policy.active_scan.socket.socket") as factory:
            with self.assertRaises((TypeError, ValueError)):
                _tcp_connect_probe(candidate, timeout_seconds=1.0)
        factory.assert_not_called()

    def test_original_host_issuance_still_enables_exact_bound_operation(self):
        self.assertTrue(request_matches_contract(self.request, self.contract))
        self.assertTrue(contract_is_current(self.contract, now_utc=self.now))
        connection = self.socket_connection()
        with patch(
            "src.obsidian_policy.active_scan.socket.socket", return_value=connection
        ) as factory:
            result = run_authorized_lab_scan(
                self.request, self.contract, audit_path=self.audit_path
            )
        self.assertTrue(result.completed)
        self.assertEqual(result.attempts, 1)
        factory.assert_called_once_with(2, 1)
        connection.connect_ex.assert_called_once_with(("127.0.0.1", 443))

    def test_same_type_contract_constructor_bypass_cannot_claim_validation(self):
        for candidate in (
            _uninitialized_clone(self.contract, seal=object()),
            _uninitialized_clone(self.contract, omit_seal=True),
            _uninitialized_clone(self.contract, seal=self.contract._seal),
        ):
            with self.subTest(candidate=candidate):
                self.assert_contract_denied(candidate)

    def test_copied_contract_seal_and_direct_construction_are_not_issuance(self):
        candidate = ValidatedExecutionContract(**_values(self.contract))
        self.assert_contract_denied(candidate)

    def test_contract_copies_and_changed_replacements_are_not_issuance(self):
        for candidate in (
            copy(self.contract),
            replace(self.contract),
            replace(self.contract, target=replace(self.contract.target, ports=(22,))),
        ):
            with self.subTest(candidate=candidate):
                self.assert_contract_denied(candidate)
        self.assertTrue(contract_is_current(self.contract, now_utc=self.now))

    def test_contract_lookalike_and_subclass_are_rejected_by_deep_consumers(self):
        class ContractSubclass(ValidatedExecutionContract):
            pass

        for candidate in (
            SimpleNamespace(**_values(self.contract)),
            _uninitialized_clone(
                self.contract, cls=ContractSubclass, seal=self.contract._seal
            ),
        ):
            with self.subTest(candidate=candidate):
                self.assert_contract_denied(candidate)

    def test_reflected_changes_to_issued_contract_invalidate_saved_values(self):
        for field_name, change in (
            ("target", lambda value: replace(value, ports=(22,))),
            ("limits", lambda value: replace(value, max_connections=64)),
            ("request_hash", lambda _value: "0" * 64),
            ("_seal", lambda _value: object()),
        ):
            with self.subTest(field=field_name):
                candidate = self.issue_contract()
                object.__setattr__(candidate, field_name, change(getattr(candidate, field_name)))
                self.assert_contract_denied(candidate)

    def test_reflected_changes_inside_issued_target_and_limits_are_detected(self):
        for nested_name, field_name, replacement in (
            ("target", "ports", (22,)),
            ("limits", "max_connections", 64),
        ):
            with self.subTest(nested=nested_name):
                candidate = self.issue_contract()
                object.__setattr__(getattr(candidate, nested_name), field_name, replacement)
                self.assert_contract_denied(candidate)

    def test_payload_byte_limit_rejects_boolean_and_float_zero(self):
        for value in (False, 0.0):
            with self.subTest(value=value):
                self.raw["limits"]["max_bytes_sent"] = value
                with self.assertRaises(ContractValidationError):
                    self.issue_contract()

    def test_validator_and_validity_recheck_require_zero_utc_offset(self):
        shifted = self.now.astimezone(timezone(timedelta(hours=2)))
        self.assertEqual(shifted, self.now)
        with self.assertRaisesRegex(ContractValidationError, "timezone-aware UTC"):
            validate_execution_contract(
                self.raw, bound_request=self.request, now_utc=shifted
            )
        with self.assertRaisesRegex(ContractValidationError, "timezone-aware UTC"):
            contract_is_current(self.contract, now_utc=shifted)

    def test_same_type_probe_constructor_bypass_never_creates_socket(self):
        original = self.issue_probe()
        for candidate in (
            _uninitialized_clone(original, seal=object()),
            _uninitialized_clone(original, omit_seal=True),
            _uninitialized_clone(original, seal=original._seal),
        ):
            with self.subTest(candidate=candidate):
                self.assert_probe_denied(candidate)

    def test_probe_copy_and_direct_construction_with_copied_seal_are_denied(self):
        original = self.issue_probe()
        for candidate in (
            copy(original),
            replace(original),
            _AuthorizedProbe(
                address=original.address,
                port=original.port,
                transport=original.transport,
                _seal=original._seal,
            ),
        ):
            with self.subTest(candidate=candidate):
                self.assert_probe_denied(candidate)
        connection = self.socket_connection()
        with patch(
            "src.obsidian_policy.active_scan.socket.socket", return_value=connection
        ) as factory:
            self.assertTrue(_tcp_connect_probe(original, timeout_seconds=1.0))
        factory.assert_called_once()

    def test_probe_lookalike_and_subclass_never_create_socket(self):
        class ProbeSubclass(_AuthorizedProbe):
            pass

        original = self.issue_probe()
        for candidate in (
            SimpleNamespace(**_values(original)),
            _uninitialized_clone(original, cls=ProbeSubclass, seal=original._seal),
        ):
            with self.subTest(candidate=candidate):
                self.assert_probe_denied(candidate)

    def test_consumed_probe_copy_replace_and_reflected_reset_cannot_replay(self):
        original = self.issue_probe()
        with patch(
            "src.obsidian_policy.active_scan.socket.socket",
            return_value=self.socket_connection(),
        ) as factory:
            _tcp_connect_probe(original, timeout_seconds=1.0)
        factory.assert_called_once()
        self.assert_probe_denied(copy(original))
        self.assert_probe_denied(replace(original))
        object.__setattr__(original, "_used", False)
        self.assert_probe_denied(original)

    def test_reflected_probe_target_or_seal_changes_burn_original_issuance(self):
        for field_name, change in (
            ("port", 22),
            ("address", "127.0.0.2"),
            ("transport", "udp"),
            ("_seal", object()),
        ):
            with self.subTest(field=field_name):
                authorization = self.issue_probe()
                saved = getattr(authorization, field_name)
                object.__setattr__(authorization, field_name, change)
                self.assert_probe_denied(authorization)
                self.assertTrue(authorization._used)
                self.assertNotIn(id(authorization), active_scan._ISSUED_PROBES)
                object.__setattr__(authorization, field_name, saved)
                object.__setattr__(authorization, "_used", False)
                self.assert_probe_denied(authorization)

    def test_helper_independently_rechecks_loopback_and_tcp_before_socket(self):
        # Host-only fault injection keeps the issuance record consistent so the
        # deeper helper boundary, rather than the preceding content gate, runs.
        for field_name, change in (("address", "192.0.2.1"), ("transport", "udp")):
            with self.subTest(field=field_name):
                authorization = self.issue_probe()
                object.__setattr__(authorization, field_name, change)
                identity = id(authorization)
                with active_scan._PROBE_ISSUANCE_LOCK:
                    record = active_scan._ISSUED_PROBES[identity]
                    active_scan._ISSUED_PROBES[identity] = (
                        record[0],
                        (authorization.address, authorization.port, authorization.transport),
                    )
                with patch("src.obsidian_policy.active_scan.socket.socket") as factory:
                    with self.assertRaisesRegex(ValueError, "fixed host boundary"):
                        _tcp_connect_probe(authorization, timeout_seconds=1.0)
                factory.assert_not_called()
                self.assert_probe_denied(authorization)

    def test_socket_error_burns_issuance_before_contact_and_prevents_replay(self):
        authorization = self.issue_probe()
        with patch(
            "src.obsidian_policy.active_scan.socket.socket",
            side_effect=OSError("synthetic socket creation error"),
        ) as factory:
            with self.assertRaises(OSError):
                _tcp_connect_probe(authorization, timeout_seconds=1.0)
        factory.assert_called_once()
        self.assert_probe_denied(authorization)
        self.assert_probe_denied(replace(authorization))

    def test_imitated_execution_guard_grants_are_denied_in_actual_scan_path(self):
        original = self.issue_probe()
        for candidate in (
            _uninitialized_clone(original, seal=object()),
            _uninitialized_clone(original, seal=original._seal),
        ):
            with self.subTest(candidate=candidate):
                imitated_guard = MagicMock()
                imitated_guard.reserve_probe.return_value = (None, candidate)
                imitated_guard.remaining_seconds.return_value = 10.0
                imitated_guard.attempts = 1
                with patch(
                    "src.obsidian_policy.active_scan.ExecutionGuard",
                    return_value=imitated_guard,
                ), patch("src.obsidian_policy.active_scan.socket.socket") as factory:
                    with self.assertRaises(TypeError):
                        run_authorized_lab_scan(
                            self.request, self.contract, audit_path=self.audit_path
                        )
                imitated_guard.reserve_probe.assert_called_once_with(
                    address="127.0.0.1", port=443, transport="tcp"
                )
                factory.assert_not_called()

    def test_unbound_reserve_cannot_mint_from_lookalike_or_exact_new_guard(self):
        legitimate = ExecutionGuard(self.contract)
        attributes = {
            name: getattr(legitimate, name)
            for name in ExecutionGuard.__slots__
            if name != "__weakref__"
        }
        forged = object.__new__(ExecutionGuard)
        for name, value in attributes.items():
            object.__setattr__(forged, name, value)
        for candidate in (
            SimpleNamespace(**attributes, current_abort_status=lambda: None),
            forged,
            copy(legitimate),
        ):
            with self.subTest(candidate=candidate):
                before = candidate._attempts
                with patch("src.obsidian_policy.active_scan.socket.socket") as factory:
                    with self.assertRaisesRegex(TypeError, "host constructor"):
                        ExecutionGuard.reserve_probe(
                            candidate, address="127.0.0.1", port=443, transport="tcp"
                        )
                    with self.assertRaisesRegex(TypeError, "host constructor"):
                        ExecutionGuard.current_abort_status(candidate)
                self.assertEqual(candidate._attempts, before)
                factory.assert_not_called()

    def test_issued_guard_cannot_swap_to_another_genuine_contract(self):
        guard = ExecutionGuard(self.contract)
        another = self.issue_contract()
        object.__setattr__(guard, "_contract", another)
        with self.assertRaisesRegex(TypeError, "host constructor"):
            guard.reserve_probe(address="127.0.0.1", port=443, transport="tcp")
        self.assertEqual(guard.attempts, 0)

    def test_concurrent_consume_permits_at_most_one_socket_creation(self):
        authorization = self.issue_probe()
        barrier = Barrier(4)

        def attempt():
            barrier.wait(timeout=5)
            try:
                return _tcp_connect_probe(authorization, timeout_seconds=1.0)
            except (TypeError, ValueError):
                return False

        with patch(
            "src.obsidian_policy.active_scan.socket.socket",
            return_value=self.socket_connection(),
        ) as factory, ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _item: attempt(), range(4)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 3)
        factory.assert_called_once()

    def test_issuance_records_do_not_retain_released_contracts_or_probes(self):
        contract = self.issue_contract()
        authorization = self.issue_probe()
        contract_identity, probe_identity = id(contract), id(authorization)
        contract_ref, probe_ref = ref(contract), ref(authorization)
        self.assertIn(contract_identity, execution_contract._ISSUED_CONTRACTS)
        self.assertIn(probe_identity, active_scan._ISSUED_PROBES)
        del contract, authorization
        gc.collect()
        self.assertIsNone(contract_ref())
        self.assertIsNone(probe_ref())
        self.assertNotIn(contract_identity, execution_contract._ISSUED_CONTRACTS)
        self.assertNotIn(probe_identity, active_scan._ISSUED_PROBES)

    def test_guard_issuance_record_does_not_retain_released_guard(self):
        guard = ExecutionGuard(self.contract)
        identity, guard_ref = id(guard), ref(guard)
        self.assertIn(identity, active_scan._ISSUED_GUARDS)
        del guard
        gc.collect()
        self.assertIsNone(guard_ref())
        self.assertNotIn(identity, active_scan._ISSUED_GUARDS)


if __name__ == "__main__":
    unittest.main()
