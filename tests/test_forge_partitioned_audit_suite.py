import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.audit_acl_contract import EXPECTED_SOURCE as ACL_SOURCE
from src.forge_runtime.durable_audit_contract import EXPECTED_SOURCE as AUDIT_SOURCE
from src.forge_runtime.partitioned_audit_suite import (
    ACL_PARTS, ACL_PART_FILES, AUDIT_PARTS, AUDIT_PART_FILES,
    run_partitioned_audit_acl, run_partitioned_durable_audit,
)
from src.forge_runtime.task_runner import parse_task_spec


class TestPartitionedAuditSuite(unittest.TestCase):
    def test_frozen_specs_and_tasks_match_all_named_fragments(self):
        root = Path(__file__).resolve().parents[1]
        for name, paths, parts in (
            ("acl", ACL_PART_FILES, ACL_PARTS),
            ("durable", AUDIT_PART_FILES, AUDIT_PARTS),
        ):
            with self.subTest(name=name):
                task = parse_task_spec((root / f"examples/forge_partitioned_{name}.json").read_text(encoding="utf-8"))
                text = (root / f"examples/forge_partitioned_{name}_SPEC.md").read_text(encoding="utf-8")
                fragments = tuple(block.split("\n```", 1)[0] + "\n" for block in text.split("```text\n")[1:])
                self.assertEqual(task.task.allowed_files, paths)
                self.assertEqual(fragments, parts)

    def test_bounded_fragments_reconstruct_exact_reviewed_sources(self):
        self.assertEqual("".join(ACL_PARTS), ACL_SOURCE)
        self.assertEqual("".join(AUDIT_PARTS), AUDIT_SOURCE)
        self.assertTrue(all(len(part) <= 2200 for part in ACL_PARTS + AUDIT_PARTS))

    def test_acl_fragments_pass_complete_native_acceptance(self):
        self.assertIsNone(run_partitioned_audit_acl(dict(zip(ACL_PART_FILES, ACL_PARTS))))

    def test_audit_fragments_and_dependency_pass_complete_composed_acceptance(self):
        contents = dict(zip(AUDIT_PART_FILES, AUDIT_PARTS))
        contents["agent/audit_acl.py"] = ACL_SOURCE
        self.assertIsNone(run_partitioned_durable_audit(contents))

    def test_changed_fragment_never_reaches_native_execution(self):
        contents = dict(zip(ACL_PART_FILES, ACL_PARTS))
        contents[ACL_PART_FILES[0]] += "unexpected code"
        with patch("src.forge_runtime.partitioned_audit_suite.run_audit_acl") as execute:
            self.assertIn("source block 1", run_partitioned_audit_acl(contents))
            execute.assert_not_called()

    def test_swapped_fragments_are_rejected(self):
        contents = dict(zip(AUDIT_PART_FILES, AUDIT_PARTS))
        contents[AUDIT_PART_FILES[0]], contents[AUDIT_PART_FILES[1]] = contents[AUDIT_PART_FILES[1]], contents[AUDIT_PART_FILES[0]]
        self.assertIn("source block 1", run_partitioned_durable_audit(contents))

    def test_missing_append_line_is_rejected_before_storage_execution(self):
        contents = dict(zip(AUDIT_PART_FILES, AUDIT_PARTS))
        contents["agent/audit_acl.py"] = ACL_SOURCE
        contents[AUDIT_PART_FILES[2]] = contents[AUDIT_PART_FILES[2]].replace(
            "    _append_entry_line(path, entry)\n", "", 1
        )
        with patch("src.forge_runtime.partitioned_audit_suite.run_durable_audit") as execute:
            failure = run_partitioned_durable_audit(contents)
            self.assertIn("source block 3", failure)
            self.assertIn("first differing line 1", failure)
            self.assertIn("_append_entry_line(path, entry)", failure)
            self.assertIn("_remove_pending_intent(path)", failure)
            self.assertIn(
                repr("    _append_entry_line(path, entry)\n    _remove_pending_intent(path)\n\n"),
                failure,
            )
            execute.assert_not_called()

    def test_missing_final_newline_is_diagnosed_without_execution(self):
        contents = dict(zip(ACL_PART_FILES, ACL_PARTS))
        contents[ACL_PART_FILES[-1]] = contents[ACL_PART_FILES[-1]].rstrip("\n")
        with patch("src.forge_runtime.partitioned_audit_suite.run_audit_acl") as execute:
            self.assertIn("first differing line", run_partitioned_audit_acl(contents))
            execute.assert_not_called()

    def test_missing_final_line_is_diagnosed_without_execution(self):
        contents = dict(zip(ACL_PART_FILES, ACL_PARTS))
        contents[ACL_PART_FILES[-1]] = "".join(ACL_PARTS[-1].splitlines(True)[:-1])
        with patch("src.forge_runtime.partitioned_audit_suite.run_audit_acl") as execute:
            failure = run_partitioned_audit_acl(contents)
            self.assertIn("actual None", failure)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
