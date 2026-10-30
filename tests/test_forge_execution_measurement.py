"""File-backed evidence gates; no model or network calls in these tests."""

from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import benchmark_execution_contract as benchmark
from src.forge_runtime.capability_registry_suite import REQUIRED_FILES as REGISTRY_FILES
from src.forge_runtime.task_runner import parse_task_spec


REAL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME = [{
    "identifier": "forge-30b", "modelKey": "qwen3-coder-30b-a3b-instruct",
    "contextLength": 32768, "parallel": 1, "status": "idle", "queued": 0,
    "quantization": {"name": "Q4_K_M"},
}]
VALIDATOR_FILES = ("agent/execution_contract.py",) + REGISTRY_FILES


def json_file(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def pipeline_report(approved=True):
    return {
        "approved": approved,
        "results": [{"role": role, "passed": approved, "summary": "fixture"}
                    for role in ("builder", "tester", "reviewer")],
    }


class ExecutionReferenceBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(dir=REAL_ROOT / "workspaces")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for source in ("src/obsidian_policy/execution_contract.py", "src/obsidian_policy/active_scan.py",
                       "examples/forge_execution_contract_SPEC.md", "examples/forge_active_execution_SPEC.md"):
            target = self.root / source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((REAL_ROOT / source).read_bytes())
        current = patch.object(benchmark, "ROOT", self.root)
        current.start()
        self.addCleanup(current.stop)

    def test_both_actual_originals_and_specifications_match_preaccepted_references(self):
        benchmark.validate_stage_reference("validator")
        benchmark.validate_stage_reference("active")

    def test_changed_original_reference_or_specification_stops_before_model_or_origin(self):
        from src.forge_runtime import execution_contract_contract, active_execution_contract
        for stage, name, original, module in (
            ("validator", "execution_contract", "execution_contract.py", execution_contract_contract),
            ("active", "active_execution", "active_scan.py", active_execution_contract),
        ):
            paths = [self.root / "src/obsidian_policy" / original,
                     self.root / ("examples/forge_" + name + "_SPEC.md")]
            for index, path in enumerate(paths):
                original_bytes = path.read_bytes()
                if index == 0:
                    mutations = [original_bytes + b"\n"]
                else:
                    mutations = [original_bytes.replace(b"```python", b"```text"),
                                 original_bytes.replace(b"The original source bytehash is", b"Changed source hash is"),
                                 original_bytes.replace(b"return", b"yield", 1)]
                for changed in mutations:
                    path.write_bytes(changed)
                    with self.subTest(stage=stage, file=path.name), self.assertRaises(ValueError):
                        benchmark.frozen_inputs(stage)
                path.write_bytes(original_bytes)
            for attribute, changed in (("REFERENCE_SHA256", "a" * 64),
                                       ("EXPECTED_SOURCE", module.EXPECTED_SOURCE + "\n")):
                with self.subTest(stage=stage, field=attribute), patch.object(module, attribute, changed), \
                        self.assertRaises(ValueError):
                    benchmark.frozen_inputs(stage)


class ExecutionEvidenceGateTests(unittest.TestCase):
    """Synthetic metadata exercises real file/hash/schema gate behavior."""

    def setUp(self):
        self.directory = TemporaryDirectory(dir=REAL_ROOT / "workspaces")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "workspaces").mkdir()
        (self.root / "examples").mkdir()
        self.hashes = {"fixed-checker.py": "d" * 64}
        self.native_runner = Mock(return_value=None)
        self.origin = self.root / "workspaces/registry-origin"
        origin_run = self.origin / "run-1"
        origin_run.mkdir(parents=True)
        self.registry_inputs = {}
        for name in REGISTRY_FILES:
            data = ("# independent file gate fixture: " + name + "\n").encode()
            self.registry_inputs[name] = data
            path = origin_run / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        origin_result = {
            "success": True,
            "artifact_sha256": {name: benchmark.digest(data)
                                for name, data in self.registry_inputs.items()},
        }
        json_file(origin_run / "result.json", origin_result)
        json_file(self.origin / "report.json", {"runs": [origin_result] * 3})
        for stage_name in ("execution_contract", "active_execution"):
            (self.root / ("examples/forge_" + stage_name + "_SPEC.md")).write_bytes(
                ("# fixture " + stage_name + "\n").encode()
            )
        # Reference-part generation is real even in this synthetic metadata fixture.
        reference_directory = self.root / "src/obsidian_policy"
        reference_directory.mkdir(parents=True)
        for source_name in ("execution_contract.py", "active_scan.py"):
            (reference_directory / source_name).write_bytes(
                (REAL_ROOT / "src/obsidian_policy" / source_name).read_bytes()
            )
        for name, value in (
            ("ROOT", self.root), ("ORIGIN", self.origin),
            ("ORIGIN_REPORT_SHA256", benchmark.digest((self.origin / "report.json").read_bytes())),
        ):
            current = patch.object(benchmark, name, value)
            current.start()
            self.addCleanup(current.stop)
        for current in (
            patch.object(benchmark, "validate_stage_reference"),
            patch.object(benchmark, "verifier_hashes", return_value=self.hashes),
            patch.object(benchmark, "stage_suite", return_value=(VALIDATOR_FILES, "execution_contract_v1", self.native_runner)),
            patch.object(benchmark, "LMStudioChatClient", side_effect=AssertionError("model call forbidden")),
            patch.object(benchmark, "runtime_snapshot", side_effect=AssertionError("network/runtime call forbidden")),
        ):
            current.start()
            self.addCleanup(current.stop)
        self.inputs = benchmark.frozen_inputs("validator")
        self.initial_path = self.root / "workspaces/initial/report.json"
        self.initial_review = self.initial_path.parent / "review.json"
        self.initial_result = self.write_run(self.initial_path.parent / "run-1")
        self.initial = self.report([self.initial_result])
        self.save_initial()
        self.series_path = self.root / "workspaces/repeats/report.json"
        self.series_review = self.series_path.parent / "review.json"
        results = [self.write_run(self.series_path.parent / f"run-{index}")
                   for index in range(1, 4)]
        self.series = self.report(results)
        self.refresh_initial_chain()
        self.save_series()

    def write_run(self, root):
        root.mkdir(parents=True)
        for name, data in self.inputs.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (root / "agent/execution_contract.py").write_bytes(b"# validator fixture\n")
        for name in ("model_calls.jsonl", "trace.jsonl"):
            (root / name).write_text('{"fixture":true}\n', encoding="utf-8")
        result = {
            "workspace": root.relative_to(self.root).as_posix(),
            **dict.fromkeys(("approved", "independent_passed", "inputs_unchanged",
                             "readback_passed", "conditions_unchanged", "success"), True),
            "pipeline": pipeline_report(), "manual_interventions": 0,
            "artifact_sha256": {name: benchmark.digest((root / name).read_bytes())
                                for name in VALIDATOR_FILES},
            **{name + "_sha256": benchmark.digest((root / name).read_bytes())
               for name in ("model_calls.jsonl", "trace.jsonl")},
        }
        json_file(root / "result.json", result)
        return result

    def report(self, runs):
        return {
            "stage": "validator", "model": "forge-30b", "temperature": 0,
            "conditions_unchanged": True, "verifier_sha256": self.hashes,
            "input_sha256": {name: benchmark.digest(data) for name, data in self.inputs.items()},
            "runtime_before": deepcopy(RUNTIME), "runtime_after": deepcopy(RUNTIME),
            "runs": runs,
        }

    def save_initial(self, *, reviewer="independent fixture reviewer"):
        json_file(self.initial_path, self.initial)
        run = self.root / self.initial_result["workspace"]
        json_file(self.initial_review, {
            "kind": "forge_execution_initial_review", "stage": "validator",
            "approved": True, "reviewer": reviewer,
            "report_sha256": benchmark.digest(self.initial_path.read_bytes()),
            "result_sha256": benchmark.digest((run / "result.json").read_bytes()),
            "model_calls_sha256": benchmark.digest((run / "model_calls.jsonl").read_bytes()),
        })

    def refresh_initial_chain(self):
        self.series["initial_acceptance"] = {
            "report": self.initial_path.relative_to(self.root).as_posix(),
            "review": self.initial_review.relative_to(self.root).as_posix(),
            "report_sha256": benchmark.digest(self.initial_path.read_bytes()),
            "review_sha256": benchmark.digest(self.initial_review.read_bytes()),
        }

    def save_series(self, *, reviewer="independent fixture reviewer"):
        json_file(self.series_path, self.series)
        json_file(self.series_review, {
            "kind": "forge_execution_series_review", "stage": "validator",
            "approved": True, "reviewer": reviewer,
            "report_sha256": benchmark.digest(self.series_path.read_bytes()),
        })

    def initial_gate(self):
        return benchmark.validate_initial_report(
            "validator", self.initial_path, self.initial_review, self.inputs, self.hashes
        )

    def active_gate(self):
        return benchmark.frozen_inputs("active", self.series_path, self.series_review)

    def test_complete_independently_bound_initial_and_three_unique_repeats_enable_next_stage(self):
        self.assertTrue(self.initial_gate())
        self.native_runner.reset_mock()
        inputs = self.active_gate()
        self.assertEqual(inputs["agent/execution_contract.py"], b"# validator fixture\n")
        self.assertEqual(self.native_runner.call_count, 4)

    def test_three_repeats_cannot_enable_next_stage_without_initial_chain(self):
        del self.series["initial_acceptance"]
        self.save_series()
        with self.assertRaises(ValueError):
            self.active_gate()
        self.native_runner.assert_not_called()

    def test_initial_and_repeat_reports_require_exactly_one_and_three_runs(self):
        original_initial = self.initial["runs"]
        for count in (0, 2):
            with self.subTest(kind="initial", count=count):
                self.initial["runs"] = [deepcopy(self.initial_result) for _ in range(count)]
                self.save_initial()
                with self.assertRaises(ValueError):
                    self.initial_gate()
        self.initial["runs"] = original_initial
        self.save_initial()
        self.refresh_initial_chain()
        original_series = self.series["runs"]
        for count in (0, 1, 2, 4):
            with self.subTest(kind="repeats", count=count):
                self.series["runs"] = (original_series * 2)[:count]
                self.save_series()
                with self.assertRaises(ValueError):
                    self.active_gate()

    def test_three_positive_repeats_cannot_hide_negative_initial_pipeline(self):
        self.initial_result["pipeline"]["approved"] = False
        run = self.root / self.initial_result["workspace"]
        json_file(run / "result.json", self.initial_result)
        self.save_initial()
        self.refresh_initial_chain()
        self.save_series()
        with self.assertRaises(ValueError):
            self.active_gate()
        self.native_runner.assert_not_called()

    def test_duplicate_repeat_workspace_cannot_count_as_three_fresh_builds(self):
        self.series["runs"][1] = deepcopy(self.series["runs"][0])
        self.save_series()
        with self.assertRaises(ValueError):
            self.active_gate()
        self.native_runner.assert_not_called()

    def test_initial_workspace_cannot_also_count_as_a_repeat(self):
        self.series["runs"][1] = deepcopy(self.initial_result)
        self.save_series()
        with self.assertRaises(ValueError):
            self.active_gate()

    def test_changed_initial_report_or_review_breaks_bound_series_chain(self):
        for path in (self.initial_path, self.initial_review):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaises(ValueError):
                    self.active_gate()
                path.write_bytes(original)

    def test_incomplete_or_invalid_runtime_snapshots_reject_initial(self):
        for field in ("runtime_before", "runtime_after"):
            original = deepcopy(self.initial[field])
            for invalid in (None, [], [{**RUNTIME[0], "parallel": 2}],
                            [{**RUNTIME[0], "contextLength": 4096}]):
                with self.subTest(field=field, invalid=invalid):
                    self.initial[field] = invalid
                    self.save_initial()
                    with self.assertRaises(ValueError):
                        self.initial_gate()
            del self.initial[field]
            self.save_initial()
            with self.assertRaises(ValueError):
                self.initial_gate()
            self.initial[field] = original

    def test_incomplete_or_invalid_runtime_snapshots_reject_validator_series(self):
        for field in ("runtime_before", "runtime_after"):
            original = deepcopy(self.series[field])
            for invalid in (None, [], [{**RUNTIME[0], "status": "busy"}]):
                with self.subTest(field=field, invalid=invalid):
                    self.series[field] = invalid
                    self.save_series()
                    with self.assertRaises(ValueError):
                        self.active_gate()
            del self.series[field]
            self.save_series()
            with self.assertRaises(ValueError):
                self.active_gate()
            self.series[field] = original

    def test_reviewer_must_be_exact_nonempty_text_in_both_review_gates(self):
        for value in (None, False, 1, [], {"name": "reviewer"}, "", " \t\n"):
            with self.subTest(reviewer=value, gate="initial"):
                self.save_initial(reviewer=value)
                with self.assertRaises(ValueError):
                    self.initial_gate()
            self.save_initial()
            self.refresh_initial_chain()
            with self.subTest(reviewer=value, gate="series"):
                self.save_series(reviewer=value)
                with self.assertRaises(ValueError):
                    self.active_gate()

    def test_initial_independent_review_requires_exact_schema_positive_approval_and_all_hashes(self):
        for field, invalid in (
            ("approved", False), ("approved", 1), ("kind", "different-review"),
            ("stage", "active"), ("report_sha256", "a" * 64),
            ("result_sha256", "b" * 64), ("model_calls_sha256", "c" * 64),
        ):
            with self.subTest(field=field, invalid=invalid):
                self.save_initial()
                review = json.loads(self.initial_review.read_text(encoding="utf-8"))
                review[field] = invalid
                json_file(self.initial_review, review)
                with self.assertRaises(ValueError):
                    self.initial_gate()
        self.save_initial()
        review = json.loads(self.initial_review.read_text(encoding="utf-8"))
        review["unbound_extra_field"] = True
        json_file(self.initial_review, review)
        with self.assertRaises(ValueError):
            self.initial_gate()
        self.native_runner.assert_not_called()

    def test_validator_series_independent_review_cannot_claim_wrong_stage_or_unbound_hash(self):
        for field, invalid in (
            ("approved", False), ("approved", 1), ("kind", "different-review"),
            ("stage", "active"), ("report_sha256", "a" * 64),
        ):
            with self.subTest(field=field, invalid=invalid):
                self.save_series()
                review = json.loads(self.series_review.read_text(encoding="utf-8"))
                review[field] = invalid
                json_file(self.series_review, review)
                with self.assertRaises(ValueError):
                    self.active_gate()
        self.native_runner.assert_not_called()

    def test_report_cannot_change_frozen_model_inputs_or_verifier_hashes(self):
        for field, invalid in (
            ("model", "different-model"), ("temperature", 0.5),
            ("conditions_unchanged", False), ("verifier_sha256", {}),
            ("input_sha256", {}), ("stage", "active"),
        ):
            with self.subTest(field=field):
                original = deepcopy(self.series[field])
                self.series[field] = invalid
                self.save_series()
                with self.assertRaises(ValueError):
                    self.active_gate()
                self.series[field] = original

    def test_manipulated_raw_trace_or_actual_artifact_rejects_initial_before_native_suite(self):
        run = self.root / self.initial_result["workspace"]
        for name in ("model_calls.jsonl", "trace.jsonl", "agent/execution_contract.py"):
            with self.subTest(name=name):
                path = run / name
                original = path.read_bytes()
                path.write_bytes(original + b"changed\n")
                self.native_runner.reset_mock()
                with self.assertRaises(ValueError):
                    self.initial_gate()
                self.native_runner.assert_not_called()
                path.write_bytes(original)

    def test_changed_actual_readonly_spec_rejects_even_with_rebound_review(self):
        run = self.root / self.initial_result["workspace"]
        path = run / "SPEC.md"
        path.write_bytes(b"changed specification\n")
        self.save_initial()
        with self.assertRaises(ValueError):
            self.initial_gate()

    def test_changed_actual_repeat_spec_rejects_next_stage_even_with_bound_metadata(self):
        for result in self.series["runs"]:
            path = self.root / result["workspace"] / "SPEC.md"
            original = path.read_bytes()
            path.write_bytes(b"changed repetition specification\n")
            with self.subTest(workspace=result["workspace"]), self.assertRaises(ValueError):
                self.active_gate()
            path.write_bytes(original)

    def test_report_and_stored_result_mismatch_cannot_be_accepted(self):
        run = self.root / self.initial_result["workspace"]
        changed = deepcopy(self.initial_result)
        changed["pipeline"]["results"][1]["summary"] = "different actual result"
        json_file(run / "result.json", changed)
        self.save_initial()
        with self.assertRaises(ValueError):
            self.initial_gate()
        self.native_runner.assert_not_called()

    def test_negative_pipeline_or_role_cannot_be_rescued_by_positive_native_runner(self):
        for mutation in ("pipeline", "tester", "reviewer"):
            with self.subTest(mutation=mutation):
                result = deepcopy(self.initial_result)
                if mutation == "pipeline":
                    result["pipeline"]["approved"] = False
                else:
                    for role in result["pipeline"]["results"]:
                        if role["role"] == mutation:
                            role["passed"] = False
                self.assertFalse(benchmark.accepted_result(result, VALIDATOR_FILES))
                run = self.root / result["workspace"]
                json_file(run / "result.json", result)
                with self.assertRaises(ValueError):
                    benchmark.evidence_run(run, result, VALIDATOR_FILES, self.native_runner)
                self.native_runner.assert_not_called()

    def test_raw_hashes_and_complete_artifact_hash_set_are_mandatory(self):
        for field, invalid in (
            ("artifact_sha256", {}), ("artifact_sha256", {"agent/execution_contract.py": "a" * 64}),
            ("model_calls.jsonl_sha256", None), ("trace.jsonl_sha256", "bad"),
        ):
            with self.subTest(field=field):
                result = deepcopy(self.initial_result)
                result[field] = invalid
                self.assertFalse(benchmark.accepted_result(result, VALIDATOR_FILES))

    def test_each_evidence_flag_requires_exact_true(self):
        for field in ("approved", "independent_passed", "inputs_unchanged",
                      "readback_passed", "conditions_unchanged"):
            for invalid in (False, None, 1):
                with self.subTest(field=field, value=invalid):
                    result = deepcopy(self.initial_result)
                    result[field] = invalid
                    self.assertFalse(benchmark.accepted_result(result, VALIDATOR_FILES))

    def test_current_registry_origin_is_bound_to_preaccepted_report_hash(self):
        path = self.origin / "report.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            benchmark.frozen_inputs("validator")

    def test_workspace_paths_reject_outside_workspace_and_normalize_inside(self):
        with self.assertRaises(ValueError):
            benchmark.workspace_path(self.root / "examples")
        with self.assertRaises(ValueError):
            benchmark.workspace_path("workspaces/../../outside")
        normalized = benchmark.workspace_path("workspaces/initial/../initial/run-1")
        self.assertEqual(normalized, self.initial_path.parent / "run-1")

    def test_cli_two_runs_or_ungated_three_runs_never_reach_model(self):
        for count, exception in ((2, SystemExit), (3, ValueError)):
            with self.subTest(count=count), patch("sys.stderr", new_callable=StringIO), patch("sys.argv", [
                "benchmark_execution_contract.py", "--stage", "validator",
                "--series", "unit-gate", "--runs", str(count),
            ]):
                with self.assertRaises(exception):
                    benchmark.main()

    def test_existing_output_directory_is_preserved_and_pipeline_never_starts(self):
        path = self.root / "workspaces/existing"
        path.mkdir()
        marker = path / "user-owned.txt"
        marker.write_text("keep this", encoding="utf-8")
        with patch.object(benchmark, "run_forge_task") as run:
            with self.assertRaises(FileExistsError):
                benchmark.execute_run(path, "validator", None, self.inputs, self.hashes, None)
        run.assert_not_called()
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep this")


class ExecutionMeasurementNativeSuiteTests(unittest.TestCase):
    """One real native suite distinguishes file success from failed pipeline."""

    def test_failed_pipeline_remains_failed_when_actual_eight_modules_pass(self):
        from tests.test_forge_execution_contract_suite import accepted_contents

        contents = accepted_contents()
        inputs = {name: value.encode("utf-8") for name, value in contents.items()
                  if name != "agent/execution_contract.py"}
        inputs["SPEC.md"] = b"unit-test readonly specification\n"
        spec = parse_task_spec(json.dumps({
            "task_id": "execution-measurement-negative-pipeline",
            "objective": "Local native verification of already fixed artifact fixtures",
            "allowed_files": ["agent/execution_contract.py"],
            "read_only_files": list(inputs),
            "checks": [{"kind": "host_suite", "suite": "execution_contract_v1"}],
        }))

        def failed_pipeline(workspace, *args, **kwargs):
            (workspace / "agent/execution_contract.py").write_text(
                contents["agent/execution_contract.py"], encoding="utf-8"
            )
            for name in ("model_calls.jsonl", "trace.jsonl"):
                (workspace / name).write_text('{"unit_fixture":true}\n', encoding="utf-8")
            return SimpleNamespace(approved=False)

        with TemporaryDirectory(dir=REAL_ROOT / "workspaces") as directory:
            run = Path(directory) / "run-1"
            with (
                patch.object(benchmark, "run_forge_task", side_effect=failed_pipeline),
                patch.object(benchmark, "format_run_report", return_value=json.dumps(pipeline_report(False))),
                patch.object(benchmark, "conditions_match", return_value=True),
                patch.object(benchmark, "LMStudioChatClient", side_effect=AssertionError("model forbidden")),
            ):
                result = benchmark.execute_run(run, "validator", spec, inputs, {}, None)
            self.assertTrue(result["independent_passed"], result.get("verification_error"))
            self.assertTrue(result["readback_passed"])
            self.assertTrue(result["inputs_unchanged"])
            self.assertFalse(result["approved"])
            self.assertFalse(result["success"])
            self.assertEqual(json.loads((run / "result.json").read_text(encoding="utf-8")), result)


if __name__ == "__main__":
    unittest.main()
