"""Real local inputs: no untrusted source or real user data is executed."""

import importlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch


def component(test, name):
    try:
        return importlib.import_module('src.forge_runtime.' + name)
    except ModuleNotFoundError:
        test.fail('F-001 component is not implemented: ' + name)


class TestF001InputBoundary(unittest.TestCase):
    def setUp(self):
        self.io = component(self, 'f001_read_only_inputs')
        self.context = self.io.create_f001_inputs()
        self.inputs = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.root = self.inputs.directory / 'empty'

    def test_snapshot_reads_only_selected_direct_files_and_keeps_bytes(self):
        (self.root / 'ü.TXT').write_bytes('ä\r\nx'.encode('utf-8'))
        (self.root / 'other.json').write_bytes(b'private')
        (self.root / 'sub').mkdir()
        (self.root / 'sub' / 'hidden.txt').write_bytes(b'private')
        before = (self.root / 'ü.TXT').read_bytes()
        result = self.inputs.snapshot('empty')
        self.assertEqual(result, {'root_error': None, 'files': [
            {'name': 'ü.TXT', 'raw': b'\xc3\xa4\r\nx', 'error': None}]})
        self.assertEqual((self.root / 'ü.TXT').read_bytes(), before)

    def test_invalid_ids_are_never_interpreted_as_paths(self):
        for candidate in ('../empty', 'C:\\secret', '//server/share', '', True, Path('empty'), 'a'*65):
            with self.subTest(candidate=candidate):
                self.assertEqual(self.inputs.snapshot(candidate), {'root_error': 'invalid_root', 'files': []})
        self.assertEqual(self.inputs.snapshot('unregistered'), {'root_error': 'unknown_root', 'files': []})

    def test_closed_inputs_cannot_read_retained_directory(self):
        self.context.__exit__(None, None, None)
        self.assertEqual(self.inputs.snapshot('empty'), {'root_error': 'invalid_root', 'files': []})

    def test_real_roots_cannot_be_registered_through_public_constructor(self):
        with self.assertRaises((TypeError, ValueError)):
            self.io.BoundF001Inputs(Path(__file__).resolve().parents[1])

    def test_file_limit_stops_opening_oversized_data(self):
        (self.root / 'large.txt').write_bytes(b'x'*65537)
        with patch.object(self.io.os, 'open', side_effect=AssertionError('oversized input opened')):
            result = self.inputs.snapshot('empty')
        self.assertEqual(result['files'], [{'name': 'large.txt', 'raw': None, 'error': 'limit_exceeded'}])

    def test_file_limit_accepts_exact_boundary(self):
        (self.root / 'fit.txt').write_bytes(b'x'*65536)
        self.assertEqual(len(self.inputs.snapshot('empty')['files'][0]['raw']), 65536)

    def test_total_limit_accepts_boundary_then_denies_before_open(self):
        for index in range(4):
            (self.root / f'{index}.txt').write_bytes(b'x'*65536)
        self.assertEqual(len(self.inputs.snapshot('empty')['files']), 4)
        (self.root / 'extra.txt').write_bytes(b'x')
        with patch.object(self.io.os, 'open', side_effect=AssertionError('overbudget input opened')):
            self.assertEqual(self.inputs.snapshot('empty'), {'root_error': 'limit_exceeded', 'files': []})

    def test_unselected_entries_still_count_toward_directory_budget(self):
        for index in range(64):
            (self.root / f'{index}.bin').write_bytes(b'')
        self.assertEqual(self.inputs.snapshot('empty'), {'root_error': None, 'files': []})
        (self.root / '65.bin').write_bytes(b'')
        self.assertEqual(self.inputs.snapshot('empty'), {'root_error': 'limit_exceeded', 'files': []})

    def test_file_count_budget(self):
        for index in range(32):
            (self.root / f'{index}.txt').write_bytes(b'')
        self.assertEqual(len(self.inputs.snapshot('empty')['files']), 32)
        (self.root / 'extra.txt').write_bytes(b'')
        self.assertEqual(self.inputs.snapshot('empty'), {'root_error': 'limit_exceeded', 'files': []})

    def test_hardlink_target_not_opened(self):
        sentinel = self.inputs.directory / 'sentinel.bin'
        sentinel.write_bytes(b'never read')
        os.link(sentinel, self.root / 'link.txt')
        with patch.object(self.io.os, 'open', side_effect=AssertionError('hardlink opened')):
            result = self.inputs.snapshot('empty')
        self.assertEqual(result['files'][0]['error'], 'unsafe_path')
        self.assertEqual(sentinel.read_bytes(), b'never read')

    def test_symlink_target_not_opened_when_permission_available(self):
        sentinel = self.inputs.directory / 'sentinel.bin'
        sentinel.write_bytes(b'never read')
        try:
            (self.root / 'link.txt').symlink_to(sentinel)
        except OSError as exc:
            self.skipTest('real symlink unavailable: ' + str(exc))
        with patch.object(self.io.os, 'open', side_effect=AssertionError('symlink opened')):
            result = self.inputs.snapshot('empty')
        self.assertEqual(result['files'][0]['error'], 'unsafe_path')
        self.assertEqual(sentinel.read_bytes(), b'never read')

    def test_read_error_is_partial_input_data(self):
        (self.root / 'a.txt').write_bytes(b'a')
        with patch.object(self.io.os, 'open', side_effect=PermissionError('sensitive path')):
            result = self.inputs.snapshot('empty')
        self.assertEqual(result['files'], [{'name': 'a.txt', 'raw': None, 'error': 'read_error'}])

    def test_file_replacement_is_denied_before_reading_replacement(self):
        path = self.root / 'a.txt'; path.write_bytes(b'original')
        real_open = self.io.os.open
        def replaced_open(candidate, flags):
            replacement = self.root / 'replacement.bin'; replacement.write_bytes(b'replacement')
            os.replace(replacement, path)
            return real_open(candidate, flags)
        with patch.object(self.io.os, 'open', side_effect=replaced_open):
            result = self.inputs.snapshot('empty')
        self.assertEqual(result, {'root_error': 'input_changed', 'files': []})

    def test_root_replacement_is_denied(self):
        self.root.rename(self.inputs.directory/'retained-original'); self.root.mkdir()
        self.assertEqual(self.inputs.snapshot('empty'), {'root_error': 'input_changed', 'files': []})

    def test_file_change_during_read_is_denied(self):
        path = self.root/'a.txt'; path.write_bytes(b'original')
        real_fstat = self.io.os.fstat
        calls = 0
        def changed_fstat(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_bytes(b'changed-longer')
            return real_fstat(descriptor)
        with patch.object(self.io.os, 'fstat', side_effect=changed_fstat):
            self.assertEqual(self.inputs.snapshot('empty'), {'root_error': 'input_changed', 'files': []})

    def test_root_junction_is_denied_without_opening_target(self):
        if os.name != 'nt':
            self.skipTest('Windows junction test')
        import subprocess
        retained = self.inputs.directory/'original'; self.root.rename(retained)
        target = self.inputs.directory/'junction-target'; target.mkdir()
        (target/'sentinel.txt').write_bytes(b'never read')
        result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(self.root), str(target)],
                                capture_output=True, check=False)
        if result.returncode:
            self.fail('Synthetic junction creation failed: '+str(result.returncode))
        with patch.object(self.io.os, 'open', side_effect=AssertionError('junction target opened')):
            self.assertEqual(self.inputs.snapshot('empty'), {'root_error': 'unsafe_path', 'files': []})
        self.assertEqual((target/'sentinel.txt').read_bytes(), b'never read')

    def test_name_and_alias_validation_blocks_before_open(self):
        for name in ('CON.txt', 'a.txt.', 'file:stream.txt', '../x.txt', 'x'*1025):
            self.assertFalse(self.io._valid_name(name), name)
        with patch.object(self.io, '_directory_names', return_value=['a.txt', 'A.txt']):
            self.assertEqual(self.inputs.snapshot('empty'), {'root_error': 'unsafe_path', 'files': []})

    def test_reserved_superscript_and_console_names_stop_before_device_metadata(self):
        reserved = ('COM¹', 'COM²', 'COM³', 'LPT¹', 'LPT²', 'LPT³',
                    'CONIN$', 'CONOUT$', 'CLOCK$')
        real_lstat = Path.lstat
        for device in reserved:
            for name in (device+'.txt', device.lower()+'.tar.txt'):
                metadata_attempts = []
                def guarded_lstat(path, *args, **kwargs):
                    if path.name == name:
                        metadata_attempts.append(path.name)
                        # Never delegate a device candidate to the real OS.
                        raise OSError('synthetic candidate metadata trap')
                    return real_lstat(path, *args, **kwargs)
                with self.subTest(name=ascii(name)), patch.object(self.io, '_directory_names', return_value=[name]), \
                        patch.object(Path, 'lstat', guarded_lstat), \
                        patch.object(self.io.os, 'open', side_effect=AssertionError('device open attempted')):
                    result = self.inputs.snapshot('empty')
                    self.assertEqual(result, {'root_error': 'unsafe_path', 'files': []})
                    self.assertEqual(metadata_attempts, [])
