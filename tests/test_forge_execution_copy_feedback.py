"""Field-specific copy rejection preserves files and permits model correction."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts import benchmark_execution_contract as benchmark
from src.forge_runtime.tool_loop import run_tool_loop
from src.forge_runtime.workspace_tools import WorkspaceFileTools
from tests import test_forge_execution_copy_protocol as fixtures


class ExecutionCopyFeedbackTests(unittest.TestCase):
    def test_wrong_search_text_is_identified_without_rejecting_correct_payload_and_can_be_corrected(self):
        for stage in ('validator', 'active'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                spec, _ = fixtures.ExecutionCopyProtocolTests().prepare(root, stage)
                output = spec.task.allowed_files[0]
                payloads = list(benchmark.reference_payloads(stage).items())
                first, second = payloads[0][1].decode(), payloads[1][1].decode()
                marker = benchmark.REFERENCE_APPEND_MARKER.decode()
                actions = [
                    {'action':'read_file','path':'SPEC.md'},
                    {'action':'read_file','path':payloads[0][0]},
                    {'action':'write_file','path':output,'content':first},
                    {'action':'read_file','path':payloads[1][0]},
                    {'action':'replace_text','path':output,'old':second+'wrong search','new':second},
                    {'action':'replace_text','path':output,'old':marker,'new':second},
                    {'action':'finish','passed':False,'summary':'remaining parts unfinished','findings':[]},
                ]
                calls = []
                def chat(messages):
                    index = len(calls); calls.append(index)
                    if index == 5:
                        feedback = json.loads(messages[-1][1])
                        self.assertEqual(feedback['error'], 'execution_copy_protocol_rejected')
                        self.assertFalse(feedback['old_matches'])
                        self.assertTrue(feedback['complete_payload_matches'])
                        self.assertTrue(feedback['path_matches'])
                        self.assertEqual(feedback['expected_old'], marker)
                        self.assertNotIn(second, json.dumps(feedback))
                        self.assertEqual((root / output).read_text(), first)
                    return json.dumps(actions[index])
                result = run_tool_loop('builder',spec.task,(),WorkspaceFileTools(root,spec.task),
                                       chat,max_steps=7,execution_copy_stage=stage)
                self.assertFalse(result.passed)
                self.assertEqual((root / output).read_text(), first.replace(marker,second))
                self.assertEqual(len(calls),7)

    def test_incomplete_write_feedback_does_not_supply_content_and_correction_remains_model_supplied(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spec, _ = fixtures.ExecutionCopyProtocolTests().prepare(root,'validator')
            output = spec.task.allowed_files[0]
            path, raw = next(iter(benchmark.reference_payloads('validator').items()))
            payload = raw.decode(); marker = benchmark.REFERENCE_APPEND_MARKER.decode()
            actions = [
                {'action':'read_file','path':'SPEC.md'}, {'action':'read_file','path':path},
                {'action':'write_file','path':output,'content':payload.removesuffix(marker)},
                {'action':'write_file','path':output,'content':payload},
                {'action':'finish','passed':False,'summary':'remaining parts unfinished','findings':[]},
            ]
            calls = []
            def chat(messages):
                index = len(calls); calls.append(index)
                if index == 3:
                    feedback = json.loads(messages[-1][1])
                    self.assertFalse(feedback['complete_payload_matches'])
                    self.assertTrue(feedback['path_matches'])
                    self.assertNotIn('expected_old',feedback)
                    self.assertNotIn(payload,json.dumps(feedback))
                    self.assertFalse((root / output).exists())
                return json.dumps(actions[index])
            result = run_tool_loop('builder',spec.task,(),WorkspaceFileTools(root,spec.task),
                                   chat,max_steps=5,execution_copy_stage='validator')
            self.assertFalse(result.passed)
            self.assertEqual((root / output).read_bytes(),raw)

    def test_identical_old_new_parser_rejection_keeps_copy_guidance_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spec, _ = fixtures.ExecutionCopyProtocolTests().prepare(root,'validator')
            output = spec.task.allowed_files[0]
            payloads = list(benchmark.reference_payloads('validator').items())
            first, second = payloads[0][1].decode(), payloads[1][1].decode()
            actions = [
                {'action':'read_file','path':'SPEC.md'},
                {'action':'read_file','path':payloads[0][0]},
                {'action':'write_file','path':output,'content':first},
                {'action':'read_file','path':payloads[1][0]},
                {'action':'replace_text','path':output,'old':second,'new':second},
                {'action':'finish','passed':False,'summary':'parser rejected no-op','findings':[]},
            ]
            calls = []
            def chat(messages):
                index = len(calls); calls.append(index)
                if index == 5:
                    feedback = json.loads(messages[-1][1])
                    self.assertEqual(feedback['error'],'invalid_tool_action')
                    self.assertIn('must differ',feedback['detail'])
                    self.assertEqual(feedback['expected_old'],benchmark.REFERENCE_APPEND_MARKER.decode())
                    self.assertIn('entire JSON-decoded content field',feedback['required'])
                    self.assertNotIn(second,json.dumps(feedback))
                    self.assertEqual((root / output).read_text(),first)
                return json.dumps(actions[index])
            result = run_tool_loop('builder',spec.task,(),WorkspaceFileTools(root,spec.task),
                                   chat,max_steps=6,execution_copy_stage='validator')
            self.assertFalse(result.passed)
            self.assertEqual((root / output).read_bytes(),payloads[0][1])


if __name__ == '__main__':
    unittest.main()
