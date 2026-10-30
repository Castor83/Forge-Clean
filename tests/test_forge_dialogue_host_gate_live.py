"""Offline behavioral checks for the harness, never claimed as live evidence."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import verify_dialogue_host_gate as harness
from src.forge_runtime import contracts
from src.forge_runtime.host_policy import HostOnlyDecision
from tests.test_forge_conversation_build import actions
from tests.test_forge_conversation_protocol import proposal


def requested_proposal():
    value = proposal()
    value["objective"] = 'Always return "Hallo " + unchanged input, no trimming.'
    value["checks"][0]["cases"] = [
        {"args": [name], "expected": "Hallo " + name} for name in harness.REQUIRED_NAMES]
    return value


def planner(value):
    return lambda messages: json.dumps({"reply": "Ich schlage eine genaue Textbegrüßung vor.",
                                       "questions": [], "task": value})


class DialogueHostGateHarnessTests(unittest.TestCase):
    def test_actual_cli_pipeline_positive_function_denied_completion_and_readback(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            recorder = harness.Recorder(directory)
            original = contracts.host_decision
            with contextlib.redirect_stdout(io.StringIO()):
                outcome = harness.run_interaction(directory, planner(requested_proposal()), actions(), recorder)
            self.assertIs(contracts.host_decision, original)
            self.assertEqual(outcome["state"], "failed")
            self.assertTrue(outcome["report"]["host_passed"])
            self.assertFalse(outcome["report"]["approved"])
            before = json.loads((directory / "before_confirmation.json").read_text(encoding="utf-8"))
            after = json.loads((directory / "after_confirmation.json").read_text(encoding="utf-8"))
            self.assertEqual(before["state"], "proposed")
            self.assertEqual(after["state"], "ready")
            self.assertEqual(before["proposal"], after["proposal"])
            self.assertEqual(after["confirmed_proposal_sha256"], harness.proposal_digest(before["proposal"]))
            self.assertGreaterEqual(len([e for e in recorder.host if e["input"] is True]), 1)
            self.assertTrue(any(e["kind"] == "planner" for e in recorder.calls if e["event"] == "response"))
            hashes = harness.evidence_hashes(directory)
            self.assertIn("build/agent/hello.py", hashes)
            self.assertIn("conversation.json", hashes)

    def test_proposal_missing_whitespace_case_aborts_before_confirmation_or_build(self):
        value = requested_proposal()
        value["checks"][0]["cases"].pop()
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            recorder = harness.Recorder(directory)
            original = contracts.host_decision
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(harness.HarnessAbort):
                harness.run_interaction(directory, planner(value), lambda _: self.fail("build called"), recorder)
            self.assertIs(contracts.host_decision, original)
            self.assertFalse((directory / "after_confirmation.json").exists())
            self.assertFalse((directory / "build").exists())

    def test_wrong_exact_expected_text_is_rejected(self):
        value = requested_proposal()
        value["checks"][0]["cases"][-1]["expected"] = "Hallo Ada"
        with self.assertRaisesRegex(ValueError, "contradicts"):
            harness.validate_proposal(value)

    def test_gate_retains_original_types_other_decisions_and_negative_outcomes(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = harness.Recorder(Path(folder))
            gate = harness.denying_gate(contracts.host_decision, recorder)
            self.assertFalse(gate(HostOnlyDecision.TASK_SUCCESS, True))
            self.assertFalse(gate(HostOnlyDecision.TASK_SUCCESS, False))
            self.assertTrue(gate(HostOnlyDecision.ROLE_TRANSITION, True))
            self.assertFalse(gate(HostOnlyDecision.ROLE_TRANSITION, False))
            with self.assertRaises(ValueError):
                gate("task_success", True)
            with self.assertRaises(ValueError):
                gate(HostOnlyDecision.TASK_SUCCESS, 1)
            self.assertEqual(len(recorder.host), 4)

    def test_finalized_recorder_blocks_late_response_and_preserves_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            recorder = harness.Recorder(directory)
            recorder.append("calls", {"event": "request", "id": 1})
            recorder.close()
            before = harness.evidence_hashes(directory)
            self.assertFalse(recorder.append("calls", {"event": "response", "id": 1}))
            self.assertEqual(harness.evidence_hashes(directory), before)
            self.assertEqual(len(recorder.calls), 1)

    def test_final_success_or_missing_raw_response_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            recorder = harness.Recorder(directory)
            with contextlib.redirect_stdout(io.StringIO()):
                harness.run_interaction(directory, planner(requested_proposal()), actions(), recorder)
            data = json.loads((directory / "conversation.json").read_text(encoding="utf-8"))
            tampered = copy.deepcopy(data)
            tampered["report"]["approved"] = True
            with self.assertRaisesRegex(ValueError, "final state"):
                harness.validate_outcome(tampered, recorder)
            recorder.calls.pop()
            with self.assertRaisesRegex(ValueError, "Raw evidence readback mismatch"):
                harness.validate_outcome(data, recorder)

    def test_manipulated_confirmed_request_rejected_before_model_call(self):
        class TaskFixture:
            data = {"proposal": requested_proposal(), "context_binding": {"sha256": "bound"}}
            def project_data(self):
                return {"unused": True}
        task = TaskFixture()
        envelope = {"kind": "build_context_data", "source": "host_confirmed_build",
                    "project_binding": task.data["context_binding"], "active_rules": [],
                    "confirmed_proposal": task.data["proposal"],
                    "confirmed_proposal_sha256": harness.proposal_digest(task.data["proposal"])}
        handoff = {"role": "builder", "completed_roles": [], "task_id": "conversation-build",
                   "allowed_files": ["agent/hello.py"]}
        manipulated = copy.deepcopy(task.data["proposal"])
        manipulated["objective"] = "Different objective"
        request = {"kind": "confirmed_build_request", "proposal": manipulated}
        messages = (("system", "Actual system"), ("user", json.dumps(handoff)),
                    ("user", json.dumps(envelope)), ("user", "feedback\n" + json.dumps(request)))
        with tempfile.TemporaryDirectory() as folder:
            recorder = harness.Recorder(Path(folder))
            client = harness.recorded_client(lambda _: self.fail("model called"), "build", task, recorder)
            with patch.object(harness, "effective_project_context", return_value={"rules": []}):
                with self.assertRaisesRegex(ValueError, "confirmed build envelope mismatch"):
                    client(messages)
            self.assertEqual(recorder.calls, [])

    def test_runtime_configuration_and_busy_model_rejected(self):
        valid = [{"identifier": "forge-30b", "modelKey": "qwen3-coder-30b-a3b-instruct",
                  "contextLength": 32768, "parallel": 1, "status": "idle", "queued": 0,
                  "quantization": {"name": "Q4_K_M"}}]
        self.assertEqual(harness.validate_runtime(valid), valid[0])
        for field, value in (("parallel", 2), ("contextLength", 8192), ("status", "busy"), ("queued", 1)):
            changed = copy.deepcopy(valid)
            changed[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                harness.validate_runtime(changed)

    def test_existing_workspace_or_boundary_escape_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "workspaces/used").mkdir(parents=True)
            marker = root / "workspaces/used/user.txt"
            marker.write_text("preserve", encoding="utf-8")
            with patch.object(harness, "ROOT", root):
                with self.assertRaises(ValueError):
                    harness.fresh_workspace("workspaces/used")
                with self.assertRaises(ValueError):
                    harness.fresh_workspace("outside")
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
