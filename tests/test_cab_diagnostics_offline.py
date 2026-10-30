"""Offline CAB contracts against the original parser, batch handler and host."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

try:
    from scripts import cab_diagnostics_offline as cab
except ImportError:
    cab = None

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = ROOT / "workspaces/forge-retry-final-diagnostics-20261006"
OUTPUT = "agent/retry_decision.py"
GOOD = (
    "def should_retry(error_kind, already_retried):\n"
    "    return isinstance(error_kind, str) and error_kind.strip() == "
    '"temporary" and already_retried is False\n'
)


class TestCabDiagnosticsOffline(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(cab, "new CAB offline helper is not implemented")
        self.temporary = TemporaryDirectory(dir=ROOT / "workspaces", prefix="cab-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.broken = (HISTORICAL / OUTPUT).read_bytes()

    def target(self, name="evaluation"):
        return self.directory / name

    def batch(self, replacements=None, **extra):
        value = {"action": "replace_text_batch", "path": OUTPUT,
                 "replacements": replacements or [{"old": self.broken.decode(), "new": GOOD}]}
        value.update(extra)
        return json.dumps(value, ensure_ascii=False)

    def saved_code(self, name="evaluation"):
        return (self.target(name) / OUTPUT).read_bytes()

    def test_preparation_preserves_c_bytes_and_has_fresh_identical_factual_blocks(self):
        prepared = cab.prepare_inputs(self.target("prepared"))
        original = (HISTORICAL / "request-021.json").read_bytes()
        self.assertEqual(prepared.c_request, original)
        self.assertEqual(hashlib.sha256(prepared.c_request).hexdigest(),
                         "5f7f95fde9569d39feabed5519c72fb9a2105a1d727938818acea4058a35ead3")
        self.assertEqual((self.target("prepared") / "request-C.json").read_bytes(), original)
        a, b = json.loads(prepared.a_request), json.loads(prepared.b_request)
        self.assertEqual(a["messages"][:2], b["messages"][:2])
        self.assertEqual(len(a["messages"]), 3)
        self.assertEqual(len(b["messages"]), 3)
        common = json.loads(a["messages"][1]["content"])
        task = json.loads((ROOT / "examples/forge_retry_decision.json").read_bytes())
        self.assertEqual(common["task"], task)
        self.assertEqual(len(common["task"]["checks"][2]["cases"]), 27)
        self.assertEqual(common["file"]["content"].encode(), self.broken)
        self.assertEqual(common["host_check_feedback"]["output"], prepared.host_message)
        old_payload = json.loads(original)
        self.assertEqual(prepared.host_message, json.loads(old_payload["messages"][-1]["content"])["output"])
        self.assertNotIn(GOOD, a["messages"][1]["content"])
        self.assertNotIn("response_format", a)
        schemas = b["response_format"]["json_schema"]["schema"]["anyOf"]
        self.assertEqual([item["properties"]["action"]["const"] for item in schemas],
                         ["replace_text_batch"])
        self.assertEqual((self.target("prepared") / "request-A.json").read_bytes(), prepared.a_request)
        self.assertEqual((self.target("prepared") / "request-B.json").read_bytes(), prepared.b_request)
        historical_response = (HISTORICAL / "http-response-021.json").read_bytes()
        self.assertEqual(prepared.historical_c_response, historical_response)
        self.assertEqual((self.target("prepared") / "historical-C-http-response.json").read_bytes(), historical_response)
        manifest = json.loads((self.target("prepared") / "input-manifest.json").read_bytes())
        self.assertEqual(manifest["binding_status"], "offline_prepared_not_final_source_binding")
        self.assertEqual(manifest["sources"]["historical_c_response"]["sha256"],
                         "f2b830d17c4cfbe33087517b805d5e5f93a8ee900f4ed8fb94ff03e2301af136")

    def test_c_hash_drift_is_rejected_before_any_output(self):
        changed = self.directory / "changed-request.json"
        changed.write_bytes((HISTORICAL / "request-021.json").read_bytes() + b" ")
        with patch.object(cab, "C_REQUEST_PATH", changed):
            with self.assertRaisesRegex(ValueError, "C request"):
                cab.prepare_inputs(self.target())
        self.assertFalse(self.target().exists())

    def test_task_drift_is_rejected_before_any_output(self):
        task = json.loads((ROOT / "examples/forge_retry_decision.json").read_bytes())
        task["checks"][2]["cases"][0]["expected"] = False
        changed = self.directory / "changed-task.json"
        changed.write_text(json.dumps(task), encoding="utf-8")
        with patch.object(cab, "TASK_PATH", changed):
            with self.assertRaisesRegex(ValueError, "task"):
                cab.prepare_inputs(self.target())
        self.assertFalse(self.target().exists())

    def test_endcode_drift_is_rejected(self):
        changed = self.directory / "changed-code.py"
        changed.write_bytes(self.broken + b"\n")
        with patch.object(cab, "ENDCODE_PATH", changed):
            with self.assertRaisesRegex(ValueError, "endcode"):
                cab.prepare_inputs(self.target())
        self.assertFalse(self.target().exists())

    def test_historical_response_drift_is_rejected_before_preparation(self):
        changed = self.directory / "changed-response.json"
        changed.write_bytes((HISTORICAL / "http-response-021.json").read_bytes() + b" ")
        with patch.object(cab, "HISTORICAL_C_RESPONSE_PATH", changed):
            with self.assertRaisesRegex(ValueError, "historical C response"):
                cab.prepare_inputs(self.target())
        self.assertFalse(self.target().exists())

    def test_outputs_refuse_existing_or_non_workspace_paths(self):
        cab.prepare_inputs(self.target())
        before = (self.target() / "request-C.json").read_bytes()
        with self.assertRaises(ValueError):
            cab.prepare_inputs(self.target())
        self.assertEqual((self.target() / "request-C.json").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "workspaces"):
            cab.evaluate_a(GOOD, ROOT / "scripts/cab-test-forbidden")
        self.assertFalse((ROOT / "scripts/cab-test-forbidden").exists())

    def test_a_raw_unicode_crlf_is_saved_unchanged_and_all_27_cases_pass(self):
        raw = "# Grüße und 雪\r\n" + GOOD.replace("\n", "\r\n")
        result = cab.evaluate_a(raw, self.target())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["host_detail"], "3/3 fixed checks passed")
        self.assertEqual(self.saved_code(), raw.encode("utf-8"))
        self.assertEqual((self.target() / "content.txt").read_bytes(), raw.encode("utf-8"))
        self.assertEqual(json.loads((self.target() / "result.json").read_bytes()), result)

    def test_a_markdown_fences_remain_in_saved_code_and_fail_format(self):
        raw = "```python\n" + GOOD + "```\n"
        result = cab.evaluate_a(raw, self.target())
        self.assertEqual(result["category"], "format")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(self.saved_code(), raw.encode())

    def test_a_prose_is_not_removed(self):
        raw = "Here is the repaired function:\n" + GOOD
        result = cab.evaluate_a(raw, self.target())
        self.assertEqual(result["category"], "format")
        self.assertEqual(self.saved_code(), raw.encode())

    def test_a_empty_content_fails_format_without_claiming_behavior_failures(self):
        result = cab.evaluate_a("", self.target())
        self.assertEqual(result["category"], "format")
        self.assertIsNone(result["host_passed"])
        self.assertEqual(self.saved_code(), b"")

    def test_a_over_file_budget_is_archived_but_not_applied(self):
        raw = "x" * 20001
        result = cab.evaluate_a(raw, self.target())
        self.assertEqual(result["category"], "file_budget")
        self.assertIsNone(result["host_passed"])
        self.assertEqual(self.saved_code(), self.broken)
        self.assertEqual((self.target() / "content.txt").read_bytes(), raw.encode())

    def test_a_unsafe_code_is_rejected_before_side_effect(self):
        marker = self.directory / "forbidden-side-effect.txt"
        raw = ("def should_retry(error_kind, already_retried):\n"
               f"    return open({str(marker)!r}, 'w').write('unsafe')\n")
        result = cab.evaluate_a(raw, self.target())
        self.assertEqual(result["category"], "host_structure")
        self.assertFalse(marker.exists())
        self.assertEqual(self.saved_code(), raw.encode())

    def test_a_safe_structure_with_wrong_behavior_reports_actual_case(self):
        result = cab.evaluate_a("def should_retry(error_kind, already_retried):\n    return False\n", self.target())
        self.assertEqual(result["category"], "behavior")
        self.assertIn("case 1 returned an unexpected value", result["host_detail"])

    def test_rawcontent_must_be_an_exact_utf8_encodable_string(self):
        for content in (None, b"python", 7, "\ud800"):
            with self.subTest(content=repr(content)):
                with self.assertRaises(ValueError):
                    cab.evaluate_a(content, self.target())
                self.assertFalse(self.target().exists())

    def test_raw_response_budget_is_refused_without_output(self):
        with self.assertRaisesRegex(ValueError, "raw response"):
            cab.evaluate_a("雪" * 333334, self.target())
        self.assertFalse(self.target().exists())

    def test_b_success_uses_actual_batch_result_and_original_host(self):
        result = cab.evaluate_b(self.batch(), self.target())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["handler_result"],
                         {"path": OUTPUT, "replacements": 1, "status": "batch_replaced"})
        self.assertEqual(result["replay_invocations"], 2)
        self.assertEqual(result["host_detail"], "3/3 fixed checks passed")
        self.assertEqual(self.saved_code(), GOOD.encode())

    def test_b_rejects_other_actions_without_applying_them(self):
        for action in ({"action": "write_file", "path": OUTPUT, "content": GOOD},
                       {"action": "run_check"},
                       {"action": "finish", "passed": True, "summary": "done", "findings": []}):
            with self.subTest(action=action["action"]):
                name = action["action"]
                result = cab.evaluate_b(json.dumps(action), self.target(name))
                self.assertEqual(result["category"], "unexpected_action")
                self.assertEqual(result["replay_invocations"], 0)
                self.assertEqual(self.saved_code(name), self.broken)

    def test_b_rejects_other_path_and_extra_fields(self):
        for name, content, category in (
            ("path", self.batch(path="../outside.py"), "unexpected_path"),
            ("extra", self.batch(extra=True), "format"),
        ):
            with self.subTest(name=name):
                result = cab.evaluate_b(content, self.target(name))
                self.assertEqual(result["category"], category)
                self.assertEqual(self.saved_code(name), self.broken)

    def test_b_old_equals_new_is_distinct_and_never_replayed(self):
        result = cab.evaluate_b(self.batch([{"old": "temporary", "new": "temporary"}]), self.target())
        self.assertEqual(result["category"], "old_equals_new")
        self.assertEqual(result["replay_invocations"], 0)
        self.assertEqual(self.saved_code(), self.broken)

    def test_b_actual_handler_reports_zero_or_two_occurrences(self):
        for name, old, expected in (("zero", "missing-text", 0), ("two", "already_retried", 2)):
            with self.subTest(name=name):
                result = cab.evaluate_b(self.batch([{"old": old, "new": "replacement"}]), self.target(name))
                self.assertEqual(result["category"], "batch_old_text_occurrence_count")
                self.assertEqual(result["handler_result"]["occurrences"], expected)
                self.assertEqual(result["handler_result"]["index"], 0)
                self.assertEqual(self.saved_code(name), self.broken)

    def test_b_later_batch_error_does_not_write_partial_result(self):
        changes = [{"old": self.broken.decode(), "new": GOOD},
                   {"old": "missing-text", "new": "other"}]
        result = cab.evaluate_b(self.batch(changes), self.target())
        self.assertEqual(result["handler_result"]["index"], 1)
        self.assertEqual(self.saved_code(), self.broken)
        self.assertIsNone(result["host_passed"])

    def test_b_sequential_replacements_use_intermediate_actual_content(self):
        unicode_code = "# Grüße\n" + GOOD
        changes = [{"old": self.broken.decode(), "new": GOOD}, {"old": GOOD, "new": unicode_code}]
        result = cab.evaluate_b(self.batch(changes), self.target())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["handler_result"]["replacements"], 2)
        self.assertEqual(self.saved_code(), unicode_code.encode())

    def test_b_reverted_batch_reports_no_content_change(self):
        changes = [{"old": self.broken.decode(), "new": GOOD}, {"old": GOOD, "new": self.broken.decode()}]
        result = cab.evaluate_b(self.batch(changes), self.target())
        self.assertEqual(result["category"], "batch_no_content_change")
        self.assertEqual(self.saved_code(), self.broken)
        self.assertIsNone(result["host_passed"])

    def test_b_rejects_seventeen_replacements_and_file_budget(self):
        cases = (("count", [{"old": "a", "new": "b"}] * 17, "format"),
                 ("budget", [{"old": self.broken.decode(), "new": "x" * 20001}], "file_budget"))
        for name, changes, expected in cases:
            with self.subTest(name=name):
                result = cab.evaluate_b(self.batch(changes), self.target(name))
                self.assertEqual(result["category"], expected)
                self.assertEqual(self.saved_code(name), self.broken)

    def test_b_raw_json_rejects_duplicates_and_nonfinite_constants(self):
        duplicate = self.batch()[:-1] + ',"path":"agent/retry_decision.py"}'
        invalid = self.batch()[:-1] + ',"extra":NaN}'
        for name, content in (("duplicate", duplicate), ("nan", invalid)):
            with self.subTest(name=name):
                result = cab.evaluate_b(content, self.target(name))
                self.assertEqual(result["category"], "format")
                self.assertEqual(result["replay_invocations"], 0)
                self.assertEqual(self.saved_code(name), self.broken)

    def test_b_unpaired_json_surrogate_is_format_failure_without_partial_write(self):
        raw = json.dumps({"action": "replace_text_batch", "path": OUTPUT,
                          "replacements": [{"old": self.broken.decode(), "new": "\ud800"}]},
                         ensure_ascii=True)
        result = cab.evaluate_b(raw, self.target())
        self.assertEqual(result["category"], "format")
        self.assertEqual(self.saved_code(), self.broken)
        self.assertEqual(result["replay_invocations"], 0)

    def test_b_overflowing_json_number_is_rejected_as_format(self):
        raw = self.batch()[:-1] + ',"extra":1e999}'
        result = cab.evaluate_b(raw, self.target())
        self.assertEqual(result["category"], "format")
        self.assertIn("nonfinite", result["detail"])

    def test_b_changed_code_still_requires_actual_host_success(self):
        invalid = "def should_retry(error_kind, already_retried):\n    return False\n"
        result = cab.evaluate_b(self.batch([{"old": self.broken.decode(), "new": invalid}]), self.target())
        self.assertEqual(result["handler_result"]["status"], "batch_replaced")
        self.assertEqual(result["category"], "behavior")
        self.assertEqual(result["status"], "FAIL")

    def test_foreign_replay_error_is_not_disguised_as_a_regular_result(self):
        with patch.object(cab, "run_tool_loop", side_effect=RuntimeError("unexpected replay failure")):
            with self.assertRaisesRegex(RuntimeError, "unexpected replay failure"):
                cab.evaluate_b(self.batch(), self.target())
        self.assertEqual(self.saved_code(), self.broken)
        self.assertFalse((self.target() / "result.json").exists())

    def test_offline_operations_do_not_contact_network_or_change_originals(self):
        paths = [HISTORICAL / "request-021.json", HISTORICAL / OUTPUT,
                 ROOT / "examples/forge_retry_decision.json",
                 ROOT / "workspaces/retry-final-diagnostics-freeze-20261006.json"]
        before = [path.read_bytes() for path in paths]
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            cab.prepare_inputs(self.target("prepared"))
            self.assertEqual(cab.evaluate_a(GOOD, self.target("a"))["status"], "PASS")
            self.assertEqual(cab.evaluate_b(self.batch(), self.target("b"))["status"], "PASS")
        self.assertEqual([path.read_bytes() for path in paths], before)


if __name__ == "__main__":
    unittest.main()
