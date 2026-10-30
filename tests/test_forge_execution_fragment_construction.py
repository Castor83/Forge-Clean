"""Real tool/pipeline behavior for the frozen fragment objectives; no model calls."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.benchmark_execution_contract import RecordedChat
from src.forge_runtime import task_runner, execution_contract_suite, active_execution_suite
from src.forge_runtime.execution_contract_contract import EXPECTED_SOURCE as VALIDATOR
from src.forge_runtime.active_execution_contract import EXPECTED_SOURCE as ACTIVE

ROOT = Path(__file__).resolve().parents[1]
ACCEPTED = ROOT / 'workspaces/forge-registry-reviewer-repeat-20261003/run-1'
MARKER = '# FORGE_FRAGMENT_APPEND_20261003\n'
RANGES = {
    'execution_contract': ((1,116),(117,189),(190,276),(277,343),(344,419),(420,425)),
    'active_execution': ((1,89),(90,178),(179,254),(255,337),(338,367)),
}


class FragmentConstructionTests(unittest.TestCase):
    def build(self, stage, omit_last=False):
        source = VALIDATOR if stage == 'execution_contract' else ACTIVE
        original_name = 'execution_contract' if stage == 'execution_contract' else 'active_scan'
        self.assertEqual(source.encode(), (ROOT / ('src/obsidian_policy/' + original_name + '.py')).read_bytes())
        specification = (ROOT / ('examples/forge_' + stage + '_SPEC.md')).read_text(encoding='utf-8')
        self.assertEqual(re.findall(r'```python\n(.*?)\n```', specification, re.DOTALL), [source])
        spec_json = json.loads((ROOT / ('examples/forge_' + stage + '.json')).read_text())
        spec = task_runner.parse_task_spec(json.dumps(spec_json))
        output = spec.task.allowed_files[0]
        lines = source.splitlines(keepends=True)
        fragments = [''.join(lines[a-1:b]) for a,b in RANGES[stage]]
        self.assertEqual(''.join(fragments), source)
        self.assertNotIn(MARKER, source)
        self.assertTrue(all(len(fragment) <= 3000 for fragment in fragments))
        # Objectives carry literal, unique anchors matching the frozen line ranges.
        for index, (fragment, (a,b)) in enumerate(zip(fragments,RANGES[stage],strict=True),1):
            k = 1
            while source.count(''.join(lines[a-1:a-1+k])) != 1: k += 1
            begin = ''.join(lines[a-1:a-1+k]); k = 1
            while source.count(''.join(lines[b-k:b])) != 1: k += 1
            end = ''.join(lines[b-k:b])
            instruction = (f'Fragment {index}, inclusive source lines {a}-{b}. BEGIN excerpt:\n'
                           + begin + 'END excerpt:\n' + end + 'END boundary instructions.\n')
            self.assertIn(instruction, spec_json['objective'])
            self.assertTrue(fragment.startswith(begin))
            self.assertTrue(fragment.endswith(end))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            readonly = {'SPEC.md': specification.encode(), **{
                name: (ACCEPTED / name).read_bytes() for name in execution_contract_suite.DEPENDENCIES}}
            if stage == 'active_execution':
                # Isolated fixture only; a real active series requires an accepted Forge validator.
                readonly['agent/execution_contract.py'] = VALIDATOR.encode()
            for name, raw in readonly.items():
                file = root / name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw)
            actions = [{'action':'read_file','path':'SPEC.md'}]
            actions += [{'action':'write_file','path':output,'content':fragments[0]+MARKER}]
            for index, fragment in enumerate(fragments[1:],1):
                if omit_last and index == len(fragments)-1:
                    break
                new = fragment + (MARKER if index < len(fragments)-1 else '')
                actions.append({'action':'replace_text','path':output,'old':MARKER,'new':new})
            actions += [{'action':'read_file','path':output},
                        {'action':'finish','passed':True,'summary':'fake builder completed','findings':[]}]
            offsets = {'builder':0,'tester':0,'reviewer':0}
            host_states = []
            real_suite = task_runner.run_host_suite
            def checked(identifier, workspace):
                host_states.append((root / output).read_bytes())
                return real_suite(identifier, workspace)
            def fake(messages):
                role = json.loads(messages[1][1])['role']; index = offsets[role]; offsets[role] += 1
                if role == 'builder':
                    self.assertEqual(host_states, [])
                    if index >= 2:
                        complete_parts = min(index-1,len(fragments)-(1 if omit_last else 0))
                        expected = ''.join(fragments[:complete_parts])
                        if complete_parts < len(fragments): expected += MARKER
                        self.assertEqual((root / output).read_bytes(), expected.encode())
                    return json.dumps(actions[index])
                if index == 0:
                    return '{"action":"run_check"}'
                feedback = json.loads(messages[-1][1])
                self.assertEqual(feedback['passed'], not omit_last)
                return json.dumps({'action':'finish','passed':feedback['passed'],
                                   'summary':'actual host result','findings':[feedback['output']]})
            configured = []
            fake.set_tool_actions = configured.append
            recorder = RecordedChat(fake, root)
            with patch.object(task_runner, 'run_host_suite', side_effect=checked), patch('socket.socket') as sockets:
                pipeline = task_runner.run_forge_task(root, spec, recorder, max_steps=20,
                    max_call_seconds=300, max_elapsed_seconds=900, max_repair_attempts=0)
                recorder.close()
                sockets.assert_not_called()
            self.assertEqual(pipeline.approved, not omit_last)
            self.assertEqual(offsets['builder'], len(actions))
            self.assertLessEqual(offsets['builder'],20)
            self.assertEqual(recorder.builder_starts,1)
            self.assertEqual(recorder.repair_actions,len(fragments)-(2 if omit_last else 1))
            self.assertEqual(host_states[-1], (root / output).read_bytes())
            if not omit_last:
                self.assertEqual((root / output).read_bytes(), source.encode())
                self.assertEqual(offsets['tester'],2)
                self.assertEqual(offsets['reviewer'],2)
            else:
                self.assertNotEqual((root / output).read_bytes(),source.encode())
                self.assertEqual(offsets['reviewer'],0)
            for name, raw in readonly.items(): self.assertEqual((root / name).read_bytes(),raw)
            for name in ('model_calls.jsonl','trace.jsonl'):
                entries=[json.loads(line) for line in (root / name).read_text().splitlines()]
                self.assertEqual(len(entries),recorder.model_calls)
                self.assertTrue(all(entry['callback_complete'] for entry in entries))

    def test_both_complete_fragment_builds_pass_real_pipeline_and_preserve_reference_bytes(self):
        for stage in RANGES:
            with self.subTest(stage=stage): self.build(stage)

    def test_missing_last_fragment_is_rejected_before_module_loader(self):
        for stage in RANGES:
            with self.subTest(stage=stage), patch.object(execution_contract_suite, '_load_validator') as validator, patch.object(active_execution_suite, '_load_active') as active:
                self.build(stage,omit_last=True)
                validator.assert_not_called()
                active.assert_not_called()


if __name__ == '__main__':
    unittest.main()
