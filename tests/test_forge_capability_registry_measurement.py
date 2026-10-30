import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from scripts import benchmark_capability_registry as benchmark
from scripts.benchmark_capability_registry import accepted_result
from src.forge_runtime.capability_registry_suite import REQUIRED_FILES
from src.forge_runtime.task_runner import parse_task_spec
from tests import test_forge_capability_registry_suite as fixtures

ROOT = fixtures.ROOT


class RegistryMeasurementTests(unittest.TestCase):
    def good(self):
        return {**dict.fromkeys(('approved', 'independent_passed', 'inputs_unchanged',
                                'readback_passed', 'conditions_unchanged'), True),
                'artifact_sha256': dict.fromkeys(REQUIRED_FILES, 'a' * 64)}

    def test_all_evidence_required(self):
        self.assertTrue(accepted_result(self.good()))
        for name in ('approved', 'independent_passed', 'inputs_unchanged', 'readback_passed', 'conditions_unchanged'):
            for value in (False, None, 1):
                with self.subTest(name=name, value=value):
                    result = self.good()
                    result[name] = value
                    self.assertFalse(accepted_result(result))

    def test_missing_or_invalid_artifact_readback_cannot_count_as_build(self):
        for value in (None, {}, {'agent/capability_registry.py': 'a' * 64},
                      dict.fromkeys(REQUIRED_FILES, 'wrong')):
            result = self.good()
            result['artifact_sha256'] = value
            self.assertFalse(accepted_result(result))

    def execute(self, root, *, approved=True, mutate=False, global_error=False):
        contents = fixtures.CapabilityRegistryTests().contents()
        spec = parse_task_spec((ROOT / 'examples/forge_capability_registry.json').read_text())
        inputs = {name: value.encode() for name, value in contents.items() if name != 'agent/capability_registry.py'}
        inputs['SPEC.md'] = (ROOT / 'examples/forge_capability_registry_SPEC.md').read_bytes()
        def pipeline(workspace, *args, **kwargs):
            (workspace / 'agent/capability_registry.py').write_text(contents['agent/capability_registry.py'], encoding='utf-8')
            if mutate:
                (workspace / 'SPEC.md').write_text('changed input', encoding='utf-8')
            return SimpleNamespace(approved=approved)
        with patch.object(benchmark, 'run_forge_task', side_effect=pipeline), \
             patch.object(benchmark, 'format_run_report', return_value=json.dumps({'approved': approved})), \
             patch.object(benchmark, 'verifier_hashes', return_value={}), \
             patch.object(benchmark, 'frozen_inputs', side_effect=ValueError('changed') if global_error else None, return_value=inputs):
            result = benchmark.execute_run(root, spec, inputs, {}, lambda messages: '')
        return result, inputs

    def test_rejected_pipeline_cannot_be_rescued_by_actual_positive_hostcheck(self):
        with TemporaryDirectory(dir=ROOT / 'workspaces') as directory:
            result, _ = self.execute(Path(directory) / 'run-1', approved=False)
            self.assertTrue(result['independent_passed'])
            self.assertTrue(result['readback_passed'])
            self.assertFalse(result['success'])

    def test_modified_readonly_input_remains_negative(self):
        with TemporaryDirectory(dir=ROOT / 'workspaces') as directory:
            result, _ = self.execute(Path(directory) / 'run-1', mutate=True)
            self.assertTrue(result['independent_passed'])
            self.assertFalse(result['inputs_unchanged'])
            self.assertFalse(result['success'])

    def test_global_input_error_retains_negative_result_file(self):
        with TemporaryDirectory(dir=ROOT / 'workspaces') as directory:
            root = Path(directory) / 'run-1'
            result, _ = self.execute(root, global_error=True)
            self.assertFalse(result['conditions_unchanged'])
            self.assertFalse(result['success'])
            self.assertEqual(json.loads((root / 'result.json').read_text()), result)

    def test_initial_gate_checks_review_and_actual_artifacts(self):
        with TemporaryDirectory(dir=ROOT / 'workspaces') as directory:
            root = Path(directory)
            result, inputs = self.execute(root / 'run-1')
            self.assertTrue(result['success'])
            (root / 'run-1/model_calls.jsonl').write_text('synthetic recorded call\n')
            report = {'model': 'forge-30b', 'temperature': 0, 'verifier_sha256': {},
                      'input_sha256': {name: benchmark.digest(value) for name, value in inputs.items()}, 'runs': [result]}
            path, review_path = root / 'report.json', root / 'review.json'
            path.write_text(json.dumps(report))
            review = {'kind': 'forge_registry_initial_review', 'approved': True, 'reviewer': 'isolated test reviewer',
                      'report_sha256': benchmark.digest(path.read_bytes()),
                      'result_sha256': benchmark.digest((root / 'run-1/result.json').read_bytes()),
                      'model_calls_sha256': benchmark.digest((root / 'run-1/model_calls.jsonl').read_bytes())}
            review_path.write_text(json.dumps(review))
            self.assertTrue(benchmark.validate_initial_report(path, review_path, inputs, {}))
            review['approved'] = False
            review_path.write_text(json.dumps(review))
            with self.assertRaises(ValueError):
                benchmark.validate_initial_report(path, review_path, inputs, {})
            review['approved'] = True
            review_path.write_text(json.dumps(review))
            (root / 'run-1/agent/capability_registry.py').write_text('changed output')
            with self.assertRaises(ValueError):
                benchmark.validate_initial_report(path, review_path, inputs, {})


if __name__ == '__main__':
    unittest.main()
