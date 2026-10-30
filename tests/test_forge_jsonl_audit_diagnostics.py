import unittest

from src.forge_runtime.jsonl_audit_suite import run_jsonl_audit_log
from tests.test_forge_audit_suite import AUDIT_SOURCE
from tests.test_forge_jsonl_audit_suite import AUDIT_LOG_SOURCE


class TestForgeJsonlAuditDiagnostics(unittest.TestCase):
    def test_reports_parent_lines_for_operators_and_exact_handler_together(self):
        changed = AUDIT_LOG_SOURCE.replace(
            'expected_previous = entries[-1]["entry_hash"] if entries else GENESIS_HASH',
            'if len(entries) > 0:\n'
            '        expected_previous = entries[-1]["entry_hash"]\n'
            '    else:\n'
            '        expected_previous = GENESIS_HASH',
        ).replace(
            'if raw and not raw.endswith(b"\\n"):',
            'if len(raw) > 0 and not raw.endswith(b"\\n"):',
        ).replace(
            'except (UnicodeDecodeError, json.JSONDecodeError):',
            'except Exception:',
        )
        lines = changed.splitlines()
        first_gt_line = lines.index("    if len(entries) > 0:") + 1
        second_gt_line = lines.index(
            '    if len(raw) > 0 and not raw.endswith(b"\\n"):'
        ) + 1
        handler_line = lines.index("        except Exception:") + 1

        failure = run_jsonl_audit_log(
            {
                "agent/audit_chain.py": AUDIT_SOURCE,
                "agent/audit_log.py": changed,
            }
        )

        self.assertIn(
            f'line {first_gt_line} uses unsupported syntax Gt in '
            '"len(entries) > 0"; replace the if/else assignment with '
            'expected_previous = entries[-1]["entry_hash"] if entries else '
            "GENESIS_HASH",
            failure,
        )
        self.assertIn(
            f'line {second_gt_line} uses unsupported syntax Gt in '
            '"len(raw) > 0"; replace that condition with '
            'if raw and not raw.endswith(b"\\n"):',
            failure,
        )
        self.assertIn(
            f"line {handler_line} except handler must catch exactly "
            "(UnicodeDecodeError, json.JSONDecodeError)",
            failure,
        )
        self.assertNotIn("line 0", failure)

    def test_rejects_broad_exception_without_other_structural_failures(self):
        changed = AUDIT_LOG_SOURCE.replace(
            'except (UnicodeDecodeError, json.JSONDecodeError):',
            'except Exception:',
        )

        failure = run_jsonl_audit_log(
            {
                "agent/audit_chain.py": AUDIT_SOURCE,
                "agent/audit_log.py": changed,
            }
        )

        self.assertIn(
            "except handler must catch exactly "
            "(UnicodeDecodeError, json.JSONDecodeError)",
            failure,
        )


if __name__ == "__main__":
    unittest.main()
