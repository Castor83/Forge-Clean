"""Frozen marker-bearing payloads are copied by fake models through real file tools."""
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

ROOT=Path(__file__).resolve().parents[1]
ORIGIN=ROOT/'workspaces/forge-registry-reviewer-repeat-20261003/run-1'


class ReferencePayloadTests(unittest.TestCase):
    def test_payloads_bind_pure_original_bytes_and_marker_metadata_with_readonly_rights(self):
        for stage,source in (('validator',VALIDATOR),('active',ACTIVE)):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as folder:
                pure=benchmark.reference_parts(stage);payloads=benchmark.reference_payloads(stage)
                self.assertEqual(b''.join(pure.values()),source.encode())
                self.assertEqual(len(pure),len(payloads))
                self.assertNotIn(benchmark.REFERENCE_APPEND_MARKER,source.encode())
                for index,(raw,payload) in enumerate(zip(pure.values(),payloads.values(),strict=True)):
                    self.assertEqual(payload,raw+(benchmark.REFERENCE_APPEND_MARKER if index<len(pure)-1 else b''))
                name=benchmark.STAGES[stage][0];spec=task_runner.parse_task_spec((ROOT/f'examples/forge_{name}.json').read_text())
                self.assertTrue(set(payloads).issubset(spec.task.read_only_files))
                self.assertTrue(set(payloads).isdisjoint(spec.task.allowed_files))
                workspace=WorkspaceFileTools(Path(folder),spec.task)
                for path in payloads:
                    with self.assertRaisesRegex(ValueError,'not allowed'):workspace.write_text(path,'forged payload')
                if stage=='validator':
                    inputs=benchmark.frozen_inputs(stage)
                    self.assertEqual({name:inputs[name] for name in pure},pure)
                    self.assertEqual({name:inputs[name] for name in payloads},payloads)

    def test_marker_collision_and_changed_payload_hash_are_rejected(self):
        with patch.object(benchmark,'reference_parts',return_value={'reference_parts/part-01.txt':benchmark.REFERENCE_APPEND_MARKER}),self.assertRaisesRegex(ValueError,'collides'):
            benchmark.reference_payloads('validator')
        for stage in ('validator','active'):
            with self.subTest(stage=stage):
                inputs=benchmark.reference_payloads(stage);altered=dict(inputs);first=next(iter(inputs));altered[first]+=b'forged'
                report={'stage':stage,'model':'forge-30b','temperature':0,'conditions_unchanged':True,
                        'runs':[{}],'verifier_sha256':{},'input_sha256':{name:benchmark.digest(raw) for name,raw in inputs.items()}}
                with self.assertRaisesRegex(ValueError,'inconsistent frozen conditions'):benchmark.complete_report(report,stage,1,altered,{})

    def test_actual_initial_payload_tamper_blocks_acceptance(self):
        # Isolate the actual-input gate; real artifact suites run in the pipeline cases below.
        for stage in ('validator','active'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);series=root/'workspaces/series';run=series/'run-1';run.mkdir(parents=True)
                inputs={**benchmark.reference_parts(stage),**benchmark.reference_payloads(stage)}
                for name,raw in inputs.items():
                    path=run/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
                (run/'result.json').write_bytes(b'{}');(run/'model_calls.jsonl').write_bytes(b'{}\n')
                report={'stage':stage,'model':'forge-30b','temperature':0,'conditions_unchanged':True,
                        'runs':[{'workspace':'workspaces/series/run-1'}],'verifier_sha256':{},
                        'input_sha256':{name:benchmark.digest(raw) for name,raw in inputs.items()}}
                report_path=series/'report.json';report_path.write_text(json.dumps(report))
                review={'kind':'forge_execution_initial_review','stage':stage,'approved':True,'reviewer':'isolated test fixture',
                        'report_sha256':benchmark.digest(report_path.read_bytes()),'result_sha256':benchmark.digest((run/'result.json').read_bytes()),
                        'model_calls_sha256':benchmark.digest((run/'model_calls.jsonl').read_bytes())}
                review_path=series/'review.json';review_path.write_text(json.dumps(review))
                with patch.object(benchmark,'ROOT',root),patch.object(benchmark,'validate_runtime'),patch.object(benchmark,'evidence_run'):
                    self.assertTrue(benchmark.validate_initial_report(stage,report_path,review_path,inputs,{}))
                    target=run/'reference_payloads/part-02.txt';target.write_bytes(target.read_bytes()+b'forged payload')
                    with self.assertRaisesRegex(ValueError,'actual initial input changed'):benchmark.validate_initial_report(stage,report_path,review_path,inputs,{})

    def run_pipeline(self,stage,corrupt=False):
        source=VALIDATOR if stage=='validator' else ACTIVE
        pure=benchmark.reference_parts(stage);payloads=benchmark.reference_payloads(stage)
        name=benchmark.STAGES[stage][0];spec=task_runner.parse_task_spec((ROOT/f'examples/forge_{name}.json').read_text());output=spec.task.allowed_files[0]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inputs={'SPEC.md':(ROOT/f'examples/forge_{name}_SPEC.md').read_bytes(),**pure,**payloads,
                                     **{path:(ORIGIN/path).read_bytes() for path in execution_contract_suite.DEPENDENCIES}}
            if stage=='active':inputs['agent/execution_contract.py']=VALIDATOR.encode()  # Fixture only; real stage needs accepted Forge validator.
            for path,raw in inputs.items():
                file=root/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(raw)
            actions=[{'action':'read_file','path':'SPEC.md'}]
            for index,(path,payload) in enumerate(payloads.items()):
                actions.append({'action':'read_file','path':path})
                copied=payload.decode()
                if corrupt and index==1:copied+='forged = True\n'
                actions.append({'action':'write_file','path':output,'content':copied} if index==0 else
                               {'action':'replace_text','path':output,'old':benchmark.REFERENCE_APPEND_MARKER.decode(),'new':copied})
            actions.extend([{'action':'read_file','path':output},{'action':'finish','passed':True,'summary':'fake payload copy','findings':[]}])
            offsets={'builder':0,'tester':0,'reviewer':0};checks=[];seen=[]
            def fake(messages):
                role=json.loads(messages[1][1])['role'];i=offsets[role];offsets[role]+=1
                if role=='builder':
                    self.assertEqual(checks,[])
                    if 2<=i<=2*len(payloads) and i%2==0:
                        index=i//2-1;feedback=json.loads(messages[-1][1]);seen.append(feedback['path'])
                        self.assertEqual(feedback['path'],list(payloads)[index]);self.assertEqual(feedback['content'],list(payloads.values())[index].decode());self.assertFalse(feedback['trusted'])
                        if not corrupt:
                            emitted=actions[i].get('content',actions[i].get('new'))
                            self.assertEqual(emitted,feedback['content'])
                    if i>=3 and i%2==1 and i<=2*len(payloads)-1:
                        built=(root/output).read_bytes();self.assertEqual(built.count(benchmark.REFERENCE_APPEND_MARKER),1)
                        if not corrupt:self.assertEqual(built,b''.join(list(pure.values())[:i//2])+benchmark.REFERENCE_APPEND_MARKER)
                    return json.dumps(actions[i])
                if i==0:return '{"action":"run_check"}'
                feedback=json.loads(messages[-1][1]);self.assertEqual(feedback['passed'],not corrupt)
                return json.dumps({'action':'finish','passed':feedback['passed'],'summary':'actual finding','findings':[feedback['output']]})
            fake.set_tool_actions=lambda actions:None
            recorder=benchmark.RecordedChat(fake,root);real=task_runner.run_host_suite
            def checked(identifier,workspace):checks.append((root/output).read_bytes());return real(identifier,workspace)
            with patch.object(task_runner,'run_host_suite',side_effect=checked),patch('socket.socket') as sockets:
                result=task_runner.run_forge_task(root,spec,recorder,max_steps=20,max_call_seconds=300,max_elapsed_seconds=900,max_repair_attempts=0)
                recorder.close();sockets.assert_not_called()
            self.assertEqual(result.approved,not corrupt);self.assertEqual(seen,list(payloads))
            self.assertEqual(offsets['builder'],15 if stage=='validator' else 13)
            self.assertEqual(recorder.repair_actions,len(payloads)-1);self.assertEqual(recorder.builder_starts,1)
            self.assertEqual((root/output).read_bytes().count(benchmark.REFERENCE_APPEND_MARKER),0)
            if not corrupt:
                self.assertEqual((root/output).read_bytes(),source.encode());self.assertEqual(offsets['tester'],2);self.assertEqual(offsets['reviewer'],2)
            else:self.assertEqual(offsets['reviewer'],0)
            for path,raw in inputs.items():self.assertEqual((root/path).read_bytes(),raw)
            entries=[json.loads(line) for line in (root/'model_calls.jsonl').read_text().splitlines()]
            self.assertEqual(len(entries),recorder.model_calls)

    def test_exact_payload_copies_preserve_markers_and_both_complete_reference_outputs(self):
        for stage in ('validator','active'):
            with self.subTest(stage=stage):self.run_pipeline(stage)

    def test_forged_payload_output_is_rejected_before_loaders(self):
        for stage in ('validator','active'):
            with self.subTest(stage=stage),patch.object(execution_contract_suite,'_load_validator') as validator,patch.object(active_execution_suite,'_load_active') as active:
                self.run_pipeline(stage,corrupt=True);validator.assert_not_called();active.assert_not_called()


if __name__=='__main__':unittest.main()
