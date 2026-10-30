"""Fixed full-AST reproduction plus actual bounded directory/JSON acceptance."""

import ast
import io
import json
import tokenize

from .f001_file_analyzer_contract import EXPECTED_SOURCE
from .f001_read_only_inputs import FIXTURE_NAMES, _valid_name, create_f001_inputs


REQUIRED_FILES = ('agent/file_analyzer.py', 'agent/file_analyzer_cases.json', 'agent/README.md')
_EXPECTED_AST = ast.dump(ast.parse(EXPECTED_SOURCE))
_ROOT_ERRORS = {'invalid_root', 'unknown_root', 'read_error', 'unsafe_path', 'input_changed', 'limit_exceeded'}
_FILE_ERRORS = {'invalid_utf8', 'read_error', 'unsafe_path', 'limit_exceeded'}
_SUMMARY_KEYS = ('ok_files', 'error_files', 'total_size_bytes', 'total_lines')


def _bounded_text(raw, byte_limit=80000, char_limit=20000):
    if type(raw) is not str or len(raw) > char_limit:
        raise ValueError('F-001 artifact exceeds text budget or has wrong type')
    try:
        if len(raw.encode('utf-8')) > byte_limit:
            raise ValueError('F-001 artifact exceeds byte budget')
    except UnicodeError as exc:
        raise ValueError('F-001 artifact must be UTF-8 text') from exc


def _validate_source(source):
    _bounded_text(source)
    depth = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.OP:
                if token.string in ('(', '[', '{'):
                    depth += 1
                    if depth > 32:
                        raise ValueError('F-001 parser nesting budget exceeded')
                elif token.string in (')', ']', '}'):
                    depth -= 1
        tree = ast.parse(source)
        stack = [(tree, 0)]
        count = 0
        while stack:
            node, level = stack.pop()
            count += 1
            if count > 2048 or level > 32:
                raise ValueError('F-001 AST budget exceeded')
            stack.extend((child, level+1) for child in ast.iter_child_nodes(node))
        if ast.dump(tree) != _EXPECTED_AST:
            raise ValueError('F-001 analyzer must match its complete frozen AST')
        return tree
    except (SyntaxError, tokenize.TokenError, IndentationError, RecursionError) as exc:
        raise ValueError('F-001 analyzer is not bounded valid Python') from exc


def _load_frozen_analyzer(source, snapshot_reader):
    tree = _validate_source(source)  # Loader always repeats the gate itself.
    if not callable(snapshot_reader):
        raise ValueError('F-001 requires a host snapshot binding')
    namespace = {'__builtins__': {'len': len, 'UnicodeDecodeError': UnicodeDecodeError},
                 'read_snapshot': snapshot_reader, 'report_json': _report_json}
    # Exact contract contains only one definition; no generated imports run.
    exec(compile(tree, '<forge-f001-frozen-analyzer>', 'exec'), namespace)
    return namespace['analyze_directory']


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('F-001 JSON duplicate key')
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError('F-001 JSON nonfinite number')


def _parse_json(raw, byte_limit, char_limit=20000):
    _bounded_text(raw, byte_limit, char_limit)
    # Bound JSON nesting before the recursive stdlib parser. Strings are inert.
    depth = 0
    quoted = False
    escaped = False
    for character in raw:
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in '[{':
            depth += 1
            if depth > 8:
                raise ValueError('F-001 JSON nesting budget exceeded')
        elif character in ']}':
            depth -= 1
    try:
        data = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('F-001 invalid JSON') from exc
    stack = [(data, 0)]
    nodes = 0
    while stack:
        value, level = stack.pop()
        nodes += 1
        if nodes > 2048 or level > 8:
            raise ValueError('F-001 JSON value budget exceeded')
        if type(value) is dict:
            stack.extend((item, level+1) for item in value.values())
        elif type(value) is list:
            stack.extend((item, level+1) for item in value)
    return data


def _validate_report(data):
    if (type(data) is not dict or set(data) != {'schema_version', 'status', 'root_error', 'files', 'summary'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or type(data['status']) is not str or data['status'] not in ('ok', 'partial', 'error')
            or type(data['files']) is not list or len(data['files']) > 32
            or type(data['summary']) is not dict or set(data['summary']) != set(_SUMMARY_KEYS)):
        raise ValueError('F-001 report schema mismatch')
    summary = data['summary']
    if any(type(summary[key]) is not int or summary[key] < 0 for key in _SUMMARY_KEYS):
        raise ValueError('F-001 report totals must be nonnegative exact integers')
    root_error = data['root_error']
    if root_error is not None:
        if (type(root_error) is not str or root_error not in _ROOT_ERRORS
                or data['status'] != 'error' or data['files'] or any(summary.values())):
            raise ValueError('F-001 root error report mismatch')
        return data
    counts = [0, 0, 0, 0]
    names = []
    for item in data['files']:
        if (type(item) is not dict or set(item) != {'name', 'status', 'size_bytes', 'line_count'}
                or not _valid_name(item['name']) or not item['name'].casefold().endswith('.txt')
                or type(item['status']) is not str):
            raise ValueError('F-001 file report schema mismatch')
        names.append(item['name'])
        if item['status'] == 'ok':
            if (type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= 65536
                    or type(item['line_count']) is not int or not 0 <= item['line_count'] <= item['size_bytes']):
                raise ValueError('F-001 file report values out of budget')
            counts[0] += 1; counts[2] += item['size_bytes']; counts[3] += item['line_count']
        elif item['status'] in _FILE_ERRORS and item['size_bytes'] is None and item['line_count'] is None:
            counts[1] += 1
        else:
            raise ValueError('F-001 file error report mismatch')
    if (names != sorted(names) or len(set(name.casefold() for name in names)) != len(names)
            or counts[2] > 262144 or tuple(summary[key] for key in _SUMMARY_KEYS) != tuple(counts)
            or data['status'] != ('partial' if counts[1] else 'ok')):
        raise ValueError('F-001 report status/order/totals mismatch')
    return data


def _report_json(data):
    _validate_report(data)
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))+'\n'
    if len(raw.encode('utf-8')) > 65536:
        raise ValueError('F-001 report byte budget exceeded')
    return raw


def parse_report(raw):
    return _validate_report(_parse_json(raw, 65536, 65536))


def _empty_report(error=None):
    return {'schema_version': 1, 'status': 'error' if error else 'ok', 'root_error': error,
            'files': [], 'summary': dict.fromkeys(_SUMMARY_KEYS, 0)}


def _host_expected():
    # Hand-derived fixture expectations, independently of generated code.
    def one(name, size, lines, error=None):
        data = _empty_report()
        data['files'] = [{'name': name, 'status': error or 'ok', 'size_bytes': size, 'line_count': lines}]
        data['status'] = 'partial' if error else 'ok'
        data['summary'] = dict(zip(_SUMMARY_KEYS, (0, 1, 0, 0) if error else (1, 0, size, lines)))
        return data
    mixed = one('ok.txt', 4, 1)
    mixed['files'].insert(0, {'name': 'bad.txt', 'status': 'invalid_utf8', 'size_bytes': None, 'line_count': None})
    mixed['status'] = 'partial'; mixed['summary']['error_files'] = 1
    return {'empty': _empty_report(), 'unicode': one('ä.txt', 4, 1), 'crlf': one('a.txt', 6, 2),
            'invalid_utf8': one('bad.txt', None, None, 'invalid_utf8'),
            'missing_root': _empty_report('unknown_root'), 'mixed': mixed}


def _validate_generated_cases(raw):
    data = _parse_json(raw, 80000)
    if (type(data) is not dict or set(data) != {'schema_version', 'cases'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or type(data['cases']) is not list or not 1 <= len(data['cases']) <= 16):
        raise ValueError('F-001 case schema mismatch')
    names = []
    for case in data['cases']:
        if (type(case) is not dict or set(case) != {'fixture', 'expected'}
                or type(case['fixture']) is not str or case['fixture'] not in FIXTURE_NAMES):
            raise ValueError('F-001 case fixture is not host-registered')
        names.append(case['fixture'])
        _validate_report(case['expected'])
    if len(set(names)) != len(names) or not set(FIXTURE_NAMES[:5]).issubset(names):
        raise ValueError('F-001 cases must include all mandatory fixtures exactly once')
    return data['cases']


def run_f001_file_analyzer(contents):
    try:
        if any(path not in contents for path in REQUIRED_FILES):
            raise ValueError('F-001 required artifact missing')
        source = contents[REQUIRED_FILES[0]]
        _validate_source(source)
        cases = _validate_generated_cases(contents[REQUIRED_FILES[1]])
        documentation = contents[REQUIRED_FILES[2]]
        _bounded_text(documentation)
        if not documentation.strip():
            raise ValueError('F-001 documentation is empty')
        with create_f001_inputs() as inputs:
            analyze = _load_frozen_analyzer(source, inputs.snapshot)
            for name, expected in _host_expected().items():
                if parse_report(analyze(name)) != expected:
                    raise ValueError('F-001 independent host fixture failed: '+name)
            for case in cases:
                if parse_report(analyze(case['fixture'])) != case['expected']:
                    raise ValueError('F-001 generated test case failed: '+case['fixture'])
        return None
    except (ValueError, TypeError, OSError, KeyError, UnicodeError, RecursionError) as exc:
        # Fixed short error classes; no absolute paths or untrusted exception text.
        return 'F-001 preparation acceptance failed: '+type(exc).__name__
