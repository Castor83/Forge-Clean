import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.test_forge_f001_input_boundary import component
from tests.test_forge_f001_file_analyzer_suite import contents


ROOT = Path(__file__).resolve().parents[1]


class TestF001TaskIntegration(unittest.TestCase):
    def setUp(self):
        self.contract = component(self, 'f001_file_analyzer_contract')

    def test_registered_suite_checks_all_outputs_and_readonly_spec(self):
        from src.forge_runtime import WorkspaceFileTools, parse_task_spec, run_host_suite
        with TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            spec = parse_task_spec((ROOT/'examples/forge_f001_file_analyzer.json').read_text())
            workspace = WorkspaceFileTools(root, spec.task)
            (root/'SPEC.md').write_bytes(b'read-only reference')
            for path, value in contents(self.contract.EXPECTED_SOURCE).items(): workspace.write_text(path, value)
            passed, detail = run_host_suite('f001_file_analyzer_v1', workspace)
            self.assertTrue(passed, detail)
            self.assertEqual((root/'SPEC.md').read_bytes(), b'read-only reference')
            with self.assertRaises(ValueError): workspace.write_text('SPEC.md', 'changed')

    def test_scripted_build_requires_real_host_success(self):
        from src.forge_runtime import parse_task_spec
        from src.forge_runtime.task_runner import run_forge_task, verify_forge_task
        from src.forge_runtime.tool_loop import HostCheckFailure
        spec = parse_task_spec((ROOT/'examples/forge_f001_file_analyzer.json').read_text())
        finish = {'action': 'finish', 'passed': True, 'summary': 'built', 'findings': []}
        for invalid in (False, True):
            data = contents(self.contract.EXPECTED_SOURCE)
            if invalid: data['agent/file_analyzer.py'] += '\nopen("outside", "w")\n'
            responses = [{'action': 'write_file', 'path': name, 'content': value} for name, value in data.items()]
            # Builder writes and finishes; tester/reviewer own host checks.
            responses += [finish, {'action': 'run_check'}, finish, {'action': 'run_check'}, finish]
            iterator = iter(json.dumps(value) for value in responses)
            with self.subTest(invalid=invalid), TemporaryDirectory(dir=ROOT) as directory:
                root = Path(directory); (root/'SPEC.md').write_bytes(b'read-only reference')
                chat = lambda messages: next(iterator)
                if invalid:
                    with self.assertRaises(HostCheckFailure): run_forge_task(root, spec, chat, max_steps=12)
                else:
                    result = run_forge_task(root, spec, chat, max_steps=12)
                    self.assertTrue(result.approved)
                    self.assertTrue(verify_forge_task(root, spec)[0])
                self.assertEqual((root/'SPEC.md').read_bytes(), b'read-only reference')
