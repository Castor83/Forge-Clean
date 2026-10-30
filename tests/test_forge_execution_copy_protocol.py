"""Real file tools reject incomplete model copies without synthesizing outputs."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts import benchmark_execution_contract as benchmark
from src.forge_runtime import task_runner, execution_contract_suite
from src.forge_runtime.contracts import SubagentResult
from src.forge_runtime.tool_loop import run_tool_loop, HostCheckFailure
from src.forge_runtime.workspace_tools import WorkspaceFileTools

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = ROOT / 'workspaces/forge-registry-reviewer-repeat-20261003/run-1'


class ExecutionCopyProtocolTests(unittest.TestCase):
    def prepare(self, root, stage):
        name = benchmark.STAGES[stage][0]
        spec = task_runner.parse_task_spec((ROOT / f'examples/forge_{name}.json').read_text())
        inputs = {'SPEC.md': (ROOT / f'examples/forge_{name}_SPEC.md').read_bytes(),
                  **benchmark.reference_parts(stage), **benchmark.reference_payloads(stage),
                  **{p: (ORIGIN / p).read_bytes() for p in execution_contract_suite.DEPENDENCIES}}
        if stage == 'active':
            inputs['agent/execution_contract.py'] = (ROOT / 'src/obsidian_policy/execution_contract.py').read_bytes()
        for name, raw in inputs.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        return spec, inputs

    def test_omitted_spec_and_marker_are_rejected_then_real_model_actions_complete_both_stages(self):
        for stage in ('validator', 'active'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); spec, inputs = self.prepare(root, stage)
                output = spec.task.allowed_files[0]; payloads = benchmark.reference_payloads(stage)
                actions = [{'action': 'read_file', 'path': next(iter(payloads))},
                           {'action': 'read_file', 'path': 'SPEC.md'}]
                reject_index = None
                for index, (path, raw) in enumerate(payloads.items()):
                    actions.append({'action': 'read_file', 'path': path})
                    action = ({'action': 'write_file', 'path': output, 'content': raw.decode()} if index == 0 else
                              {'action': 'replace_text', 'path': output, 'old': benchmark.REFERENCE_APPEND_MARKER.decode(), 'new': raw.decode()})
                    if index == 1:
                        actions.append({**action, 'new': raw.decode().removesuffix(benchmark.REFERENCE_APPEND_MARKER.decode())})
                        reject_index = len(actions)
                    actions.append(action)
                actions += [{'action': 'read_file', 'path': output},
                            {'action': 'finish', 'passed': True, 'summary': 'model copied all payloads', 'findings': []}]
                offsets = {'builder': 0, 'tester': 0, 'reviewer': 0}
                configured = []
                spec_seen = []; payload_requests = []
                def chat(messages):
                    role = json.loads(messages[1][1])['role']; i = offsets[role]; offsets[role] += 1
                    if role == 'builder':
                        data = [json.loads(m[1]) for m in messages[2:] if m[0] == 'user' and m[1].startswith('{')]
                        files = [d for d in data if d.get('kind') == 'untrusted_file_data']
                        if i == 2:
                            self.assertEqual([d['path'] for d in files], ['SPEC.md'])
                            self.assertEqual(files[0]['content'], inputs['SPEC.md'].decode().replace('\r\n', '\n'))
                            spec_seen.append(True)
                        if actions[i]['action'] in ('write_file', 'replace_text'):
                            self.assertIn('entire JSON-decoded content field', messages[0][1])
                            self.assertIn('unfinished Python definitions', messages[0][1])
                            self.assertIn('literal # FORGE_FRAGMENT_APPEND_20261003 marker', messages[0][1])
                            self.assertIn('write_file.content' if actions[i]['action'] == 'write_file' else 'replace_text.new', messages[0][1])
                            self.assertEqual(len(files), 1)
                            self.assertTrue(files[0]['path'].startswith('reference_payloads/'))
                            self.assertEqual(files[0]['content'], inputs[files[0]['path']].decode())
                            self.assertFalse(files[0]['trusted'])
                            payload_requests.append(files[0]['path'])
                        expected = ('finish',) if i == len(actions)-1 else (
                            ('write_file', 'finish') if i == 3 else
                            ('replace_text', 'finish') if (i >= 5 and i <= len(actions)-3 and (i == 5 or i == 6 or i % 2 == 0)) else
                            ('read_file', 'finish'))
                        self.assertEqual(configured[-1], expected)
                        if i == 1:
                            self.assertFalse((root / output).exists())
                            self.assertEqual(json.loads(messages[-1][1])['error'], 'execution_copy_protocol_rejected')
                        if i == reject_index:
                            self.assertEqual((root / output).read_bytes(), next(iter(payloads.values())))
                            self.assertEqual(json.loads(messages[-1][1])['error'], 'execution_copy_protocol_rejected')
                        return json.dumps(actions[i])
                    if i == 0: return json.dumps({'action': 'read_file', 'path': output})
                    if i == 1: return '{"action":"run_check"}'
                    feedback = json.loads(messages[-1][1]); self.assertTrue(feedback['passed'])
                    return json.dumps({'action': 'finish', 'passed': True, 'summary': 'host passed', 'findings': []})
                chat.set_tool_actions = configured.append
                with patch('socket.socket') as sockets:
                    result = task_runner.run_forge_task(root, spec, chat, max_steps=20, execution_copy_stage=stage)
                    sockets.assert_not_called()
                self.assertTrue(result.approved)
                self.assertEqual(spec_seen, [True])
                self.assertEqual(payload_requests, [p for index, p in enumerate(payloads) for _ in range(2 if index == 1 else 1)])
                original = 'execution_contract.py' if stage == 'validator' else 'active_scan.py'
                self.assertEqual((root / output).read_bytes(), (ROOT / 'src/obsidian_policy' / original).read_bytes())
                for p, raw in inputs.items(): self.assertEqual((root / p).read_bytes(), raw)
                self.assertEqual(offsets['builder'], 17 if stage == 'validator' else 15)

    def test_incomplete_copy_cannot_finish_or_write_and_uses_existing_step_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); spec, _ = self.prepare(root, 'validator'); output = root / spec.task.allowed_files[0]
            actions = iter([{'action': 'read_file', 'path': 'SPEC.md'},
                            {'action': 'read_file', 'path': 'reference_payloads/part-01.txt'},
                            {'action': 'write_file', 'path': spec.task.allowed_files[0], 'content': 'forged'},
                            {'action': 'finish', 'passed': True, 'summary': 'false claim', 'findings': []}])
            with self.assertRaisesRegex(RuntimeError, 'max_steps'):
                run_tool_loop('builder', spec.task, (), WorkspaceFileTools(root, spec.task),
                              lambda messages: json.dumps(next(actions)), max_steps=4, execution_copy_stage='validator')
            self.assertFalse(output.exists())

    def test_negative_checks_cannot_repeat_read_or_claim_success_and_model_finish_is_preserved(self):
        for role in ('tester', 'reviewer'):
            for claim in (False, True):
                with self.subTest(role=role, claim=claim), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder); spec, _ = self.prepare(root, 'validator'); calls = []; configured = []
                    actions = iter([{'action': 'run_check'}, {'action': 'run_check'},
                                    {'action': 'read_file', 'path': 'SPEC.md'},
                                    {'action': 'finish', 'passed': claim, 'summary': 'actual model verdict', 'findings': ['exact negative']}])
                    def check(): calls.append(1); return False, 'exact negative'
                    def chat(messages): return json.dumps(next(actions))
                    chat.set_tool_actions = configured.append
                    previous = (SubagentResult('builder', True, 'built', ()),)
                    if role == 'reviewer': previous += (SubagentResult('tester', True, 'tested', ()),)
                    args = dict(role=role, task=spec.task, previous_results=previous, workspace=WorkspaceFileTools(root, spec.task),
                                chat=chat, max_steps=4, check_runner=check, execution_copy_stage='validator')
                    if claim:
                        with self.assertRaises(HostCheckFailure): run_tool_loop(**args)
                    else:
                        result = run_tool_loop(**args); self.assertFalse(result.passed)
                        self.assertEqual(result.summary, 'actual model verdict'); self.assertEqual(result.findings, ('exact negative',))
                    self.assertEqual(calls, [1]); self.assertEqual(configured[-1], ('finish',))

    def test_protocol_cannot_be_enabled_with_other_task_or_missing_bound_host_suite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); spec, _ = self.prepare(root, 'validator')
            for forged in (replace(spec, task=replace(spec.task, task_id='other-task')),
                           replace(spec, checks=(task_runner.StaticCheck('file_exists', spec.task.allowed_files[0]),))):
                with self.assertRaisesRegex(ValueError, 'bound task and host suite'):
                    task_runner.run_forge_task(root, forged, lambda messages: self.fail('model must not be called'), execution_copy_stage='validator')


if __name__ == '__main__': unittest.main()
