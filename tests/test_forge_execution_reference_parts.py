"""Read-only reference parts are frozen inputs; models still supply every output byte."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import benchmark_execution_contract as benchmark
from src.forge_runtime import task_runner, execution_contract_suite, active_execution_suite
from src.forge_runtime.execution_contract_contract import EXPECTED_SOURCE as VALIDATOR
from src.forge_runtime.active_execution_contract import EXPECTED_SOURCE as ACTIVE
from src.forge_runtime.workspace_tools import WorkspaceFileTools

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = ROOT / 'workspaces/forge-registry-reviewer-repeat-20261003/run-1'
MARKER = '# FORGE_FRAGMENT_APPEND_20261003\n'


class ReferencePartTests(unittest.TestCase):
    def test_frozen_inputs_bind_all_parts_to_the_exact_original_and_task_read_rights(self):
        for stage,source in (('validator',VALIDATOR),('active',ACTIVE)):
            with self.subTest(stage=stage):
                parts = benchmark.reference_parts(stage)
                self.assertEqual(b''.join(parts.values()),source.encode())
                self.assertEqual(benchmark.digest(b''.join(parts.values())),benchmark.REFERENCE_HASHES[stage])
                name = benchmark.STAGES[stage][0]
                spec = task_runner.parse_task_spec((ROOT / f'examples/forge_{name}.json').read_text())
                self.assertTrue(set(parts).issubset(spec.task.read_only_files))
                self.assertEqual(len(parts),6 if stage == 'validator' else 5)
                self.assertEqual(spec.task.allowed_files,('agent/execution_contract.py',) if stage == 'validator' else ('agent/active_scan.py',))
                if stage == 'validator':
                    inputs = benchmark.frozen_inputs(stage)
                    self.assertEqual({name:inputs[name] for name in parts},parts)
                with tempfile.TemporaryDirectory() as folder:
                    workspace = WorkspaceFileTools(Path(folder),spec.task)
                    for name in parts:
                        with self.assertRaisesRegex(ValueError,'not allowed'):
                            workspace.write_text(name,'forged reference')

    def test_source_tamper_and_part_hash_tamper_cannot_become_frozen_acceptance(self):
        for stage,source in (('validator',VALIDATOR),('active',ACTIVE)):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                root=Path(folder); name=benchmark.STAGES[stage][0]
                original='execution_contract.py' if stage=='validator' else 'active_scan.py'
                target=root/'src/obsidian_policy'/original;target.parent.mkdir(parents=True);target.write_bytes(source.encode()+b'\nforged = True\n')
                spec=root/f'examples/forge_{name}_SPEC.md';spec.parent.mkdir();spec.write_bytes((ROOT/f'examples/forge_{name}_SPEC.md').read_bytes())
                with patch.object(benchmark,'ROOT',root), self.assertRaisesRegex(ValueError,'original source'):
                    benchmark.reference_parts(stage)
                parts=benchmark.reference_parts(stage)
                tampered=dict(parts); first=next(iter(parts));tampered[first]+=b'\nforged = True\n'
                hashes={name:benchmark.digest(raw) for name,raw in parts.items()}
                report={'stage':stage,'model':'forge-30b','temperature':0,'conditions_unchanged':True,
                        'runs':[{}],'verifier_sha256':{},'input_sha256':hashes}
                with self.assertRaisesRegex(ValueError,'inconsistent frozen conditions'):
                    benchmark.complete_report(report,stage,1,tampered,{})
                with patch.dict(benchmark.REFERENCE_PART_LINES,{stage:((1,1),)}), self.assertRaisesRegex(ValueError,'complete original'):
                    benchmark.reference_parts(stage)

    def test_actual_initial_part_file_tamper_blocks_acceptance(self):
        # Isolate the actual-input gate; full real artifact checks are covered below.
        for stage in ('validator','active'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                root=Path(folder); series=root/'workspaces/series';run=series/'run-1';run.mkdir(parents=True)
                inputs=benchmark.reference_parts(stage)
                for name,raw in inputs.items():
                    path=run/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
                (run/'result.json').write_bytes(b'{}')
                (run/'model_calls.jsonl').write_bytes(b'{}\n')
                report={'stage':stage,'model':'forge-30b','temperature':0,'conditions_unchanged':True,
                        'runs':[{'workspace':'workspaces/series/run-1'}],
                        'verifier_sha256':{},'input_sha256':{name:benchmark.digest(raw) for name,raw in inputs.items()}}
                report_path=series/'report.json';report_path.write_text(json.dumps(report))
                review={'kind':'forge_execution_initial_review','stage':stage,'approved':True,'reviewer':'isolated test fixture',
                        'report_sha256':benchmark.digest(report_path.read_bytes()),
                        'result_sha256':benchmark.digest((run/'result.json').read_bytes()),
                        'model_calls_sha256':benchmark.digest((run/'model_calls.jsonl').read_bytes())}
                review_path=series/'review.json';review_path.write_text(json.dumps(review))
                with patch.object(benchmark,'ROOT',root),patch.object(benchmark,'validate_runtime'),patch.object(benchmark,'evidence_run'):
                    self.assertTrue(benchmark.validate_initial_report(stage,report_path,review_path,inputs,{}))
                    target=run/next(iter(inputs));target.write_bytes(target.read_bytes()+b'forged part')
                    with self.assertRaisesRegex(ValueError,'actual initial input changed'):
                        benchmark.validate_initial_report(stage,report_path,review_path,inputs,{})

    def run_pipeline(self,stage,corrupt=False):
        source=VALIDATOR if stage=='validator' else ACTIVE
        parts=benchmark.reference_parts(stage);name=benchmark.STAGES[stage][0]
        spec=task_runner.parse_task_spec((ROOT/f'examples/forge_{name}.json').read_text())
        output=spec.task.allowed_files[0]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            inputs={'SPEC.md':(ROOT/f'examples/forge_{name}_SPEC.md').read_bytes(),**parts,
                    **{name:(ORIGIN/name).read_bytes() for name in execution_contract_suite.DEPENDENCIES}}
            if stage=='active': inputs['agent/execution_contract.py']=VALIDATOR.encode()  # Test fixture only.
            for name,raw in inputs.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            actions=[{'action':'read_file','path':'SPEC.md'}]
            for index,(name,raw) in enumerate(parts.items()):
                actions.append({'action':'read_file','path':name})
                text=raw.decode()
                if corrupt and index==0: text+='\nforged = True\n'
                content=text+(MARKER if index<len(parts)-1 else '')
                actions.append({'action':'write_file','path':output,'content':content} if index==0 else
                               {'action':'replace_text','path':output,'old':MARKER,'new':content})
            actions.extend([{'action':'read_file','path':output},{'action':'finish','passed':True,'summary':'fake build','findings':[]}])
            offsets={'builder':0,'tester':0,'reviewer':0};checks=[]
            def fake(messages):
                role=json.loads(messages[1][1])['role'];i=offsets[role];offsets[role]+=1
                if role=='builder':
                    self.assertEqual(checks,[])
                    if 2<=i<=2*len(parts) and i%2==0:
                        # Every output response follows the actual read_file tool feedback.
                        feedback=json.loads(messages[-1][1])
                        self.assertEqual(feedback['path'],list(parts)[i//2-1])
                        self.assertEqual(feedback['content'],list(parts.values())[i//2-1].decode())
                        self.assertFalse(feedback['trusted'])
                    return json.dumps(actions[i])
                if i==0:return '{"action":"run_check"}'
                feedback=json.loads(messages[-1][1]);self.assertEqual(feedback['passed'],not corrupt)
                return json.dumps({'action':'finish','passed':feedback['passed'],'summary':'host finding','findings':[feedback['output']]})
            fake.set_tool_actions=lambda actions:None
            recorder=benchmark.RecordedChat(fake,root)
            real=task_runner.run_host_suite
            def check(identifier,workspace):
                checks.append((root/output).read_bytes());return real(identifier,workspace)
            with patch.object(task_runner,'run_host_suite',side_effect=check),patch('socket.socket') as socket:
                result=task_runner.run_forge_task(root,spec,recorder,max_steps=20,
                    max_call_seconds=300,max_elapsed_seconds=900,max_repair_attempts=0)
                recorder.close();socket.assert_not_called()
            self.assertEqual(result.approved,not corrupt)
            self.assertEqual(offsets['builder'],15 if stage=='validator' else 13)
            self.assertEqual(recorder.repair_actions,len(parts)-1)
            self.assertEqual(recorder.builder_starts,1)
            if not corrupt:
                self.assertEqual((root/output).read_bytes(),source.encode())
                self.assertEqual(offsets['tester'],2);self.assertEqual(offsets['reviewer'],2)
            else:self.assertEqual(offsets['reviewer'],0)
            for name,raw in inputs.items():self.assertEqual((root/name).read_bytes(),raw)
            entries=[json.loads(line) for line in (root/'model_calls.jsonl').read_text().splitlines()]
            self.assertEqual(len(entries),recorder.model_calls)
            self.assertTrue(all(entry['callback_complete'] for entry in entries))

    def test_models_read_each_part_and_real_tools_build_both_byte_exact_outputs(self):
        for stage in ('validator','active'):
            with self.subTest(stage=stage):self.run_pipeline(stage)

    def test_forged_model_part_fails_ast_gate_before_loaders(self):
        for stage in ('validator','active'):
            with self.subTest(stage=stage),patch.object(execution_contract_suite,'_load_validator') as validator,patch.object(active_execution_suite,'_load_active') as active:
                self.run_pipeline(stage,corrupt=True);validator.assert_not_called();active.assert_not_called()


if __name__=='__main__':unittest.main()
