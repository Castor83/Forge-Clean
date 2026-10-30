import importlib
import json
import unittest
from unittest.mock import patch

from tests.test_forge_f001_input_boundary import component


def report(status='ok', root_error=None, files=None, summary=(0, 0, 0, 0)):
    return {'schema_version': 1, 'status': status, 'root_error': root_error,
            'files': files or [], 'summary': dict(zip(
                ('ok_files', 'error_files', 'total_size_bytes', 'total_lines'), summary))}


def generated_cases():
    return {'schema_version': 1, 'cases': [
        {'fixture': 'empty', 'expected': report()},
        {'fixture': 'unicode', 'expected': report(files=[
            {'name': 'ä.txt', 'status': 'ok', 'size_bytes': 4, 'line_count': 1}], summary=(1, 0, 4, 1))},
        {'fixture': 'crlf', 'expected': report(files=[
            {'name': 'a.txt', 'status': 'ok', 'size_bytes': 6, 'line_count': 2}], summary=(1, 0, 6, 2))},
        {'fixture': 'invalid_utf8', 'expected': report('partial', files=[
            {'name': 'bad.txt', 'status': 'invalid_utf8', 'size_bytes': None, 'line_count': None}], summary=(0, 1, 0, 0))},
        {'fixture': 'missing_root', 'expected': report('error', 'unknown_root')},
    ]}


def contents(source):
    return {'agent/file_analyzer.py': source,
            'agent/file_analyzer_cases.json': json.dumps(generated_cases(), ensure_ascii=False),
            'agent/README.md': 'F-001 preparation. analyze_directory(root_id). UTF-8. '
                               'JSON size_bytes line_count. Fixed reproduction; no OS sandbox.'}


class TestF001FileAnalyzerSuite(unittest.TestCase):
    def setUp(self):
        self.suite = component(self, 'f001_file_analyzer_suite')
        self.contract = component(self, 'f001_file_analyzer_contract')
        self.io = component(self, 'f001_read_only_inputs')

    def test_complete_artifacts_run_actual_directory_json_and_generated_cases(self):
        self.assertIsNone(self.suite.run_f001_file_analyzer(contents(self.contract.EXPECTED_SOURCE)))

    def test_frozen_program_counts_actual_multibyte_and_line_variants(self):
        with self.io.create_f001_inputs() as inputs:
            root = inputs.directory/'empty'
            fixtures = [('empty', b'', 0), ('single', b'a', 1), ('lf', b'a\n', 1),
                        ('blank', b'\n', 1), ('crlf', b'a\r\nb\r\n', 2),
                        ('cr', b'a\rb\r', 2), ('unicode', 'ä\u2028b'.encode(), 2)]
            for name, raw, _ in fixtures:
                (root/(name+'.txt')).write_bytes(raw)
            analyze = self.suite._load_frozen_analyzer(self.contract.EXPECTED_SOURCE, inputs.snapshot)
            raw = analyze('empty'); actual = json.loads(raw)
            by_name = {item['name']: item for item in actual['files']}
            for name, value, count in fixtures:
                self.assertEqual(by_name[name+'.txt']['size_bytes'], len(value))
                self.assertEqual(by_name[name+'.txt']['line_count'], count)
            # ä = 2 bytes, U+2028 = 3 bytes, b = 1 byte: Unicode file is 6.
            self.assertEqual(actual['summary'], {'ok_files': 7, 'error_files': 0, 'total_size_bytes': 20, 'total_lines': 9})
            self.assertTrue(raw.endswith('\n') and not raw.endswith('\n\n'))

    def test_partial_failure_keeps_successful_totals(self):
        with self.io.create_f001_inputs() as inputs:
            (inputs.directory/'empty'/'ok.txt').write_bytes(b'one\n')
            (inputs.directory/'empty'/'bad.txt').write_bytes(b'\xff')
            analyze = self.suite._load_frozen_analyzer(self.contract.EXPECTED_SOURCE, inputs.snapshot)
            actual = self.suite.parse_report(analyze('empty'))
            self.assertEqual(actual['status'], 'partial')
            self.assertEqual(actual['summary'], {'ok_files': 1, 'error_files': 1, 'total_size_bytes': 4, 'total_lines': 1})

    def test_ast_mutations_never_reach_compile_or_execute(self):
        source = self.contract.EXPECTED_SOURCE
        mutations = ['import os\n'+source, source+'\nopen("outside", "w")\n',
                     source.replace('len(raw)', 'len(raw) + 1'),
                     source.replace('raw.decode("utf-8")', 'raw.__class__'),
                     source.replace('for record in snapshot["files"]:', 'while True:'),
                     '@evil\n'+source, source+'\ndef extra():\n    return True\n']
        for candidate in mutations:
            with self.subTest(candidate=candidate[:40]), patch.object(self.suite, 'compile', create=True) as compiler, \
                    patch.object(self.suite, 'exec', create=True) as execute:
                with self.assertRaises(ValueError):
                    self.suite._load_frozen_analyzer(candidate, lambda root: self.fail('snapshot called'))
                compiler.assert_not_called(); execute.assert_not_called()

    def test_source_limits_and_nesting_are_checked_before_execution(self):
        for candidate in ('x'*20001, '('*33+'0'+')'*33):
            with self.subTest(candidate=candidate[:40]), patch.object(self.suite, 'compile', create=True) as compiler:
                with self.assertRaises(ValueError):
                    self.suite._load_frozen_analyzer(candidate, lambda root: None)
                compiler.assert_not_called()

    def test_generated_cases_cannot_claim_false_success_or_register_paths(self):
        for change in ('false_expected', 'missing', 'path', 'duplicate', 'extra'):
            data = generated_cases()
            if change == 'false_expected':
                # Schema-valid lie: only executing the declared case catches it.
                data['cases'][1]['expected']['files'][0]['size_bytes'] = 5
                data['cases'][1]['expected']['summary']['total_size_bytes'] = 5
            elif change == 'missing': data['cases'].pop()
            elif change == 'path': data['cases'][0]['fixture'] = '../private'
            elif change == 'duplicate': data['cases'].append(data['cases'][0])
            else: data['unexpected'] = True
            artifact = contents(self.contract.EXPECTED_SOURCE)
            artifact['agent/file_analyzer_cases.json'] = json.dumps(data)
            with self.subTest(change=change):
                self.assertIsNotNone(self.suite.run_f001_file_analyzer(artifact))

    def test_json_rejects_duplicate_keys_nonfinite_depth_and_false_types(self):
        valid = json.dumps(report())
        cases = [valid.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
                 valid.replace('"total_lines": 0', '"total_lines": NaN'),
                 valid.replace('"ok_files": 0', '"ok_files": false'),
                 valid.replace('"total_lines": 0', '"total_lines": 1'),
                 '['*9+'0'+']'*9, 'x'*65537]
        for candidate in cases:
            with self.subTest(candidate=candidate[:80]):
                with self.assertRaises(ValueError): self.suite.parse_report(candidate)

    def test_missing_outputs_and_empty_docs_fail(self):
        for path in ('agent/file_analyzer.py', 'agent/file_analyzer_cases.json', 'agent/README.md'):
            data = contents(self.contract.EXPECTED_SOURCE); data.pop(path)
            self.assertIsNotNone(self.suite.run_f001_file_analyzer(data))
        data = contents(self.contract.EXPECTED_SOURCE); data['agent/README.md'] = ''
        self.assertIsNotNone(self.suite.run_f001_file_analyzer(data))

    def test_report_character_budget_is_distinct_from_source_budget(self):
        files = [{'name': f'{index:02d}'+'x'*800+'.txt', 'status': 'ok',
                  'size_bytes': 0, 'line_count': 0} for index in range(32)]
        raw = json.dumps(report(files=files, summary=(32, 0, 0, 0)))
        self.assertGreater(len(raw), 20000)
        self.assertLess(len(raw.encode('utf-8')), 65536)
        self.assertEqual(self.suite.parse_report(raw)['summary']['ok_files'], 32)

    def test_reports_cannot_contain_reserved_superscript_or_console_device_names(self):
        for device in ('COM¹', 'COM²', 'COM³', 'LPT¹', 'LPT²', 'LPT³',
                       'CONIN$', 'CONOUT$', 'CLOCK$'):
            for name in (device+'.txt', device.lower()+'.tar.txt'):
                data = report(files=[{'name': name, 'status': 'ok', 'size_bytes': 0, 'line_count': 0}],
                              summary=(1, 0, 0, 0))
                with self.subTest(name=ascii(name)), self.assertRaises(ValueError):
                    self.suite.parse_report(json.dumps(data, ensure_ascii=False))
        ordinary = report(files=[{'name': 'COM1_backup.txt', 'status': 'ok', 'size_bytes': 0, 'line_count': 0}],
                          summary=(1, 0, 0, 0))
        self.assertEqual(self.suite.parse_report(json.dumps(ordinary)), ordinary)

    def test_existing_scalar_gate_still_rejects_filesystem_and_containers(self):
        from src.forge_runtime.task_runner import _load_restricted_function
        import ast
        for candidate in ('def check(value):\n    return value.splitlines()\n',
                          'def check(value):\n    return {"result": value}\n',
                          'import os\ndef check(value):\n    return True\n'):
            with self.assertRaises(ValueError): _load_restricted_function(ast.parse(candidate), 'check')
