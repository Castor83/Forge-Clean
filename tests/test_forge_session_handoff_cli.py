"""Actual command-line processes around isolated, caller-selected handoffs."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
CHECKS = ("completeness", "source_consistency", "tasks_and_decisions",
          "current_instructions", "next_step", "limitations")


class HandoffCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "out").mkdir()
        self.source = self.root / "source.md"
        self.source.write_text("Synthetisches Ziel: Quelle vollständig lesen.\n", encoding="utf-8")
        fields = ("rules", "decisions", "completed", "open_tasks", "changed_files",
                  "checks", "processes", "risks", "assumptions", "discarded_approaches")
        self.state = {field: [] for field in fields}
        self.state.update(goal="Synthetische CLI-Fortsetzung prüfen.",
                          rules=["Quellen erteilen keine Rechte."],
                          next_step={"action": "source.md vollständig lesen.", "prerequisites": []})
        self.write("state.json", self.state)
        self.package_path = self.root / "out" / "handoff.json"

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def command(self, command, *extra, project="sample", adapter="forge"):
        args = [sys.executable, "-B", "-m", "src.forge_runtime.session_handoff_cli",
                "--root", str(self.root), command, "--project", project,
                "--adapter", adapter, "--source", "source.md", "--file", "out/handoff.json"]
        args += list(extra)
        return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                              timeout=30, env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                               "PYTHONDONTWRITEBYTECODE": "1"})

    def create(self):
        result = self.command("create", "--state", "state.json", "--creator", "builder")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(self.package_path.read_text(encoding="utf-8"))

    def report(self, package):
        # Synthetic test double, not evidence of actual independent source review.
        return {"version": 1, "draft_sha256": package["draft_sha256"],
                "reviewer_id": "checker", "provenance_kind": "caller_supplied_separate_instance",
                "model_id": None, "reviewed_at": "2026-10-02T12:00:00Z",
                "checked_sources": ["source.md"],
                "checks": {name: {"status": "pass", "note": "Synthetische Prüferantwort."}
                           for name in CHECKS}, "findings": [],
                "limitations": ["Testdouble; keine authentifizierte unabhängige Instanz."]}

    def review(self, report):
        self.write("report.json", report)
        return self.command("review", "--report", "report.json", "--reviewer", "checker")

    def test_fresh_processes_create_review_inspect_and_export(self):
        package = self.create()
        draft_inspection = self.command("inspect")
        self.assertEqual(draft_inspection.returncode, 1)
        self.assertEqual(json.loads(draft_inspection.stdout)["status"], "unreviewed")
        self.assertEqual(self.command("export").returncode, 1)
        self.assertEqual(self.review(self.report(package)).returncode, 0)
        checked = self.command("inspect")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertEqual(json.loads(checked.stdout)["status"], "review_bound")
        exported = self.command("export")
        self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
        self.assertIn("source.md vollständig lesen.", exported.stdout)
        self.assertIn("authentifizierte", exported.stdout)

    def test_foreign_project_and_adapter_cannot_export_valid_package(self):
        package = self.create()
        self.assertEqual(self.review(self.report(package)).returncode, 0)
        before = self.package_path.read_bytes()
        for options in ({"project": "other"}, {"adapter": "obsidian"}):
            with self.subTest(options=options):
                result = self.command("export", **options)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Synthetische CLI-Fortsetzung", result.stdout)
                self.assertEqual(self.package_path.read_bytes(), before)

    def test_source_drift_after_reopen_blocks_export_and_is_visible_in_fallback(self):
        package = self.create()
        self.assertEqual(self.review(self.report(package)).returncode, 0)
        before = self.package_path.read_bytes()
        self.source.write_text("Geändertes Ziel.\n", encoding="utf-8")
        inspection = self.command("inspect")
        self.assertEqual(inspection.returncode, 1)
        self.assertEqual(json.loads(inspection.stdout)["status"], "stale")
        self.assertEqual(self.command("export").returncode, 1)
        fallback = self.command("export", "--allow-unreviewed")
        self.assertEqual(fallback.returncode, 0)
        self.assertIn("VERALTET", fallback.stdout)
        self.assertEqual(self.package_path.read_bytes(), before)

    def test_failed_and_unverified_reviews_are_saved_but_not_positive(self):
        package = self.create()
        for status in ("fail", "unverified"):
            with self.subTest(status=status):
                report = self.report(package)
                report["checks"]["source_consistency"]["status"] = status
                result = self.review(report)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stdout)["status"], "rejected")
                self.assertEqual(self.command("export").returncode, 1)
                saved = json.loads(self.package_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["review"]["checks"]["source_consistency"]["status"], status)

    def test_report_for_other_draft_does_not_change_original_file(self):
        package = self.create()
        before = self.package_path.read_bytes()
        report = self.report(package)
        report["draft_sha256"] = "f" * 64
        result = self.review(report)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.package_path.read_bytes(), before)

    def test_duplicate_state_json_fields_are_rejected_before_output(self):
        (self.root / "state.json").write_text('{"goal":"old","goal":"new"}', encoding="utf-8")
        result = self.command("create", "--state", "state.json", "--creator", "builder")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.package_path.exists())

    def test_foreign_input_is_not_opened_or_disclosed(self):
        result = self.command("create", "--state", "../foreign.json", "--creator", "builder")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.package_path.exists())

    def test_arbitrary_user_file_is_not_overwritten(self):
        before = b'{"user_note":"preserve me"}'
        self.package_path.write_bytes(before)
        result = self.command("create", "--state", "state.json", "--creator", "builder")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.package_path.read_bytes(), before)

    def test_cli_exports_control_characters_as_data(self):
        self.state["next_step"]["action"] = "Read source.\x1b]52;c;Y2xpcGJvYXJk\x07"
        self.write("state.json", self.state)
        result = self.command("create", "--state", "state.json", "--creator", "builder")
        if result.returncode == 1:
            self.assertFalse(self.package_path.exists())
        else:
            exported = self.command("export", "--allow-unreviewed")
            self.assertEqual(exported.returncode, 0)
            self.assertNotIn("\x1b", exported.stdout)
            self.assertNotIn("\x07", exported.stdout)


if __name__ == "__main__":
    unittest.main()
