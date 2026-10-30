"""Actual client request bytes and real file tools for copy field ordering."""
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import benchmark_execution_contract as benchmark
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.tool_loop import run_tool_loop
from src.forge_runtime.workspace_tools import WorkspaceFileTools
from tests import test_forge_execution_copy_protocol as fixtures


class ExecutionFieldOrderTests(unittest.TestCase):
    def capture(self, enabled=False, actions=None):
        requests = []
        def opener(request, timeout):
            requests.append(request.data)
            return io.BytesIO(json.dumps({'choices': [{'message': {
                'content': '{"action":"finish","passed":false,"summary":"stop","findings":[]}'
            }}]}).encode())
        client = LMStudioChatClient('fake', opener=opener,
                                    execution_copy_field_order=enabled)
        if actions is not None:
            client.set_tool_actions(actions)
        client((('user', 'test'),))
        return requests[0], json.loads(requests[0])

    def test_actual_serialization_changes_only_order_and_default_stays_canonical(self):
        default_raw, default = self.capture()
        ordered_raw, ordered = self.capture(True)
        self.assertEqual(default, ordered)
        self.assertEqual(default_raw, json.dumps(default, ensure_ascii=True,
                         sort_keys=True, separators=(',', ':')).encode())
        schemas = ordered['response_format']['json_schema']['schema']['anyOf']
        by_action = {s['properties']['action']['const']: s for s in schemas}
        self.assertEqual(list(by_action['write_file']['properties']), ['action','path','content'])
        self.assertEqual(list(by_action['replace_text']['properties']), ['action','path','old','new'])
        self.assertEqual(by_action['write_file']['properties']['content'], {'type':'string'})
        self.assertEqual(by_action['replace_text']['properties']['new'], {'type':'string'})
        self.assertEqual(by_action['replace_text']['properties']['old'], {'minLength':1,'type':'string'})
        self.assertNotEqual(default_raw, ordered_raw)
        # Apart from these two mappings even serialized byte order stays fixed.
        for schema in schemas:
            schema['properties'] = dict(sorted(schema['properties'].items()))
        self.assertEqual(json.dumps(ordered,ensure_ascii=True,separators=(',',':')).encode(),
                         default_raw)
        # Opt-in must not mutate the shared schema for subsequent default calls.
        self.assertEqual(self.capture()[0], default_raw)
        for action in ('write_file','replace_text'):
            _, subset = self.capture(True, (action,'finish'))
            self.assertEqual([s['properties']['action']['const'] for s in
                subset['response_format']['json_schema']['schema']['anyOf']], [action,'finish'])

    def test_opt_in_rejects_non_boolean_and_other_response_kinds(self):
        for value in (None, 1, 'true'):
            with self.assertRaises(ValueError):
                LMStudioChatClient('fake', execution_copy_field_order=value)
        for kind in ('subagent_result','conversation','handoff_draft','handoff_review'):
            with self.assertRaises(ValueError):
                LMStudioChatClient('fake', response_kind=kind, execution_copy_field_order=True)

    def test_serialized_copy_requests_drive_real_temporary_pipeline_for_both_stages(self):
        for stage in ('validator','active'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                spec, inputs = fixtures.ExecutionCopyProtocolTests().prepare(root,stage)
                output = spec.task.allowed_files[0]
                payloads = list(benchmark.reference_payloads(stage).items())
                actions = [{'action':'read_file','path':'SPEC.md'}]
                for index,(path,raw) in enumerate(payloads):
                    actions.append({'action':'read_file','path':path})
                    actions.append({'action':'write_file','path':output,'content':raw.decode()}
                        if index == 0 else {'action':'replace_text','path':output,
                            'old':benchmark.REFERENCE_APPEND_MARKER.decode(),'new':raw.decode()})
                actions += [{'action':'read_file','path':output},
                            {'action':'finish','passed':True,'summary':'copied','findings':[]}]
                captured = []
                def opener(request, timeout):
                    payload = json.loads(request.data)
                    action = actions[len(captured)]
                    schemas = payload['response_format']['json_schema']['schema']['anyOf']
                    by_action = {s['properties']['action']['const']:s for s in schemas}
                    self.assertIn(action['action'],by_action)
                    if action['action'] in ('write_file','replace_text'):
                        self.assertEqual(list(by_action[action['action']]['properties']),
                            ['action','path','content'] if action['action']=='write_file'
                            else ['action','path','old','new'])
                        data = json.loads(payload['messages'][-1]['content'])
                        self.assertEqual(data['content'],action.get('content',action.get('new')))
                        self.assertFalse(data['trusted'])
                    captured.append(request.data)
                    return io.BytesIO(json.dumps({'choices':[{'message':{
                        'content':json.dumps(action)}}]}).encode())
                client = LMStudioChatClient('fake',opener=opener,execution_copy_field_order=True)
                result = run_tool_loop('builder',spec.task,(),WorkspaceFileTools(root,spec.task),
                    client,max_steps=20,execution_copy_stage=stage)
                self.assertTrue(result.passed)
                self.assertEqual(len(captured),len(actions))
                expected = b''.join(benchmark.reference_parts(stage).values())
                self.assertEqual((root/output).read_bytes(),expected)
                self.assertTrue(all((root/p).read_bytes()==raw for p,raw in inputs.items()))


if __name__ == '__main__':
    unittest.main()
