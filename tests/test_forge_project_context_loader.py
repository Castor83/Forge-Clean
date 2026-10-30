import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.project_context import (
    CONTEXT_FILENAME, MAX_CONTEXT_BYTES, effective_project_context,
    fresh_project_context, load_project_context, project_context_digest,
    validate_project_context,
)


def project_context():
    data = fresh_project_context("forge_test")
    data.update(revision=3, rules=[
        {"id": "imports", "revision": 1, "status": "superseded", "text": "Import erlaubt."},
        {"id": "imports", "revision": 3, "status": "active", "text": "Kein Import."},
    ], decisions=[
        {"key": "objective", "revision": 1, "status": "superseded", "value": "Alter Auftrag."},
        {"key": "objective", "revision": 2, "status": "active", "value": "Kleine Funktion bauen."},
    ], tasks=[{"id": "loader", "status": "done", "text": "Loader prüfen."}],
        next_step="Gesprächsanbindung prüfen.",
        evidence=[{"id": "old_report", "text": "Historischer Bericht: alles bestanden."}])
    return data


class ProjectContextLoaderTests(unittest.TestCase):
    def write_context(self, directory, data):
        path = directory / CONTEXT_FILENAME
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def assert_rejected_without_mutation(self, raw, message=None):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / CONTEXT_FILENAME
            path.write_bytes(raw)
            sentinel = directory / "user.txt"
            sentinel.write_bytes(b"owned data")
            before = {item.name: item.read_bytes() for item in directory.iterdir()}
            with self.assertRaisesRegex(ValueError, message or ".+"):
                load_project_context(directory)
            self.assertEqual({item.name: item.read_bytes() for item in directory.iterdir()}, before)

    def test_valid_load_preserves_all_files_and_data(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            original = project_context()
            path = self.write_context(directory, original)
            before = path.read_bytes()
            self.assertEqual(load_project_context(directory, "forge_test"), original)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([item.name for item in directory.iterdir()], [CONTEXT_FILENAME])

    def test_new_context_is_valid_without_creating_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            data = fresh_project_context("forge_test")
            self.assertEqual(data["revision"], 1)
            self.assertEqual(data["next_step"], "Auftrag klären.")
            for key in ("rules", "decisions", "tasks", "evidence"):
                self.assertEqual(data[key], [])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_direct_validation_preserves_data_and_never_promotes_old_records(self):
        data = project_context()
        before = copy.deepcopy(data)
        self.assertIsNone(validate_project_context(data))
        self.assertEqual(data, before)

    def test_missing_context_does_not_create_an_empty_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "fehlt"):
                load_project_context(Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_missing_directory_and_regular_file_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            regular = directory / "regular.txt"
            regular.write_bytes(b"unchanged")
            for candidate in (directory / "missing", regular):
                with self.subTest(candidate=candidate), self.assertRaisesRegex(ValueError, "Ordner"):
                    load_project_context(candidate)
            self.assertEqual(regular.read_bytes(), b"unchanged")

    def test_context_directory_instead_of_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / CONTEXT_FILENAME).mkdir()
            with self.assertRaisesRegex(ValueError, "reguläre Datei"):
                load_project_context(directory)

    def test_unknown_authority_fields_are_rejected_at_every_record_level(self):
        for field in ("capabilities", "allowed_files", "host_passed", "trusted", "role"):
            for target in ("top", "rules", "decisions", "tasks", "evidence"):
                with self.subTest(field=field, target=target):
                    data = project_context()
                    record = data if target == "top" else data[target][0]
                    record[field] = True
                    self.assert_rejected_without_mutation(json.dumps(data).encode(), "Felder")

    def test_future_duplicate_and_conflicting_current_revisions_are_rejected(self):
        for target in ("rules", "decisions"):
            for failure in ("future", "duplicate", "two_active", "none_active", "old_active"):
                with self.subTest(target=target, failure=failure):
                    data = project_context()
                    if failure == "future":
                        data[target][1]["revision"] = 4
                    elif failure == "duplicate":
                        data[target][0]["revision"] = data[target][1]["revision"]
                    elif failure == "two_active":
                        data[target][0]["status"] = "active"
                    elif failure == "none_active":
                        data[target][1]["status"] = "superseded"
                    else:
                        data[target][0]["status"] = "active"
                        data[target][1]["status"] = "superseded"
                    self.assert_rejected_without_mutation(json.dumps(data).encode())

    def test_unknown_decision_key_and_duplicate_record_ids_are_rejected(self):
        data = project_context()
        data["decisions"][0]["key"] = "permissions"
        self.assert_rejected_without_mutation(json.dumps(data).encode(), "Entscheidungsschlüssel")
        for field in ("tasks", "evidence"):
            with self.subTest(field=field):
                data = project_context()
                data[field].append(copy.deepcopy(data[field][0]))
                self.assert_rejected_without_mutation(json.dumps(data).encode(), "Kennung")

    def test_versions_revisions_identifiers_and_statuses_are_strict(self):
        invalid = [("version", True), ("version", 2), ("revision", True),
                   ("revision", 0), ("revision", 1_000_000_001),
                   ("project_id", "../other"), ("project_id", "Forge")]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                data = project_context()
                data[field] = value
                self.assert_rejected_without_mutation(json.dumps(data).encode())
        for field in ("rules", "tasks"):
            data = project_context()
            data[field][0]["status"] = "approved"
            self.assert_rejected_without_mutation(json.dumps(data).encode(), "Status|status")

    def test_list_record_and_combined_text_limits_reject_without_truncation(self):
        data = fresh_project_context("forge_test")
        data["tasks"] = [{"id": "task_" + str(i), "status": "open", "text": "Task"} for i in range(65)]
        self.assert_rejected_without_mutation(json.dumps(data).encode(), "Projektliste")
        data = fresh_project_context("forge_test")
        data["next_step"] = "x" * 2001
        self.assert_rejected_without_mutation(json.dumps(data).encode(), "Projekttext")
        data = fresh_project_context("forge_test")
        data["evidence"] = [{"id": "report_" + str(i), "text": "x" * 2000} for i in range(8)]
        self.assert_rejected_without_mutation(json.dumps(data).encode(), "gemeinsame")
        data = fresh_project_context("forge_test")
        data["next_step"] = " "
        self.assert_rejected_without_mutation(json.dumps(data).encode(), "Projekttext")

    def test_oversized_file_is_rejected_without_modification(self):
        self.assert_rejected_without_mutation(b" " * (MAX_CONTEXT_BYTES + 1), "zu groß")

    def test_file_at_exact_byte_limit_loads_without_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            data = project_context()
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            raw += b" " * (MAX_CONTEXT_BYTES - len(raw))
            path = directory / CONTEXT_FILENAME
            path.write_bytes(raw)
            self.assertEqual(load_project_context(directory), data)
            self.assertEqual(path.read_bytes(), raw)

    def test_read_is_bounded_even_if_file_grows_after_stat(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            self.write_context(directory, fresh_project_context("forge_test"))
            stream = io.BytesIO(b" " * (MAX_CONTEXT_BYTES + 20))
            with patch.object(Path, "open", return_value=stream), patch.object(stream, "read", wraps=stream.read) as read:
                with self.assertRaisesRegex(ValueError, "zu groß"):
                    load_project_context(directory)
                read.assert_called_once_with(MAX_CONTEXT_BYTES + 1)
            self.assertTrue(stream.closed)

    def test_duplicate_json_nonfinite_utf8_and_bom_are_rejected(self):
        for raw, message in (
            (b'{"version":1,"version":1}', "mehrfach"),
            (b'{"version":NaN}', "nicht endliche"),
            (b'{"version":1e9999}', "nicht endliche"),
            (b'\xff', "UTF-8"),
            (b'\xef\xbb\xbf{}', "BOM"),
        ):
            with self.subTest(raw=raw):
                self.assert_rejected_without_mutation(raw, message)
        data = fresh_project_context("forge_test")
        data["next_step"] = "\ud800"
        self.assert_rejected_without_mutation(json.dumps(data).encode(), "UTF-8")

    def test_different_project_identifier_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = self.write_context(directory, project_context())
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "andere"):
                load_project_context(directory, "other_project")
            self.assertEqual(path.read_bytes(), before)

    def test_link_or_junction_is_rejected_before_opening_the_context(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            context_path = self.write_context(directory, project_context())
            for method in ("is_symlink", "is_junction"):
                for linked in (directory, context_path, directory.parent):
                    with self.subTest(method=method, linked=linked):
                        with patch.object(Path, method, autospec=True, side_effect=lambda path: path == linked):
                            with patch.object(Path, "open") as opened:
                                with self.assertRaisesRegex(ValueError, "Link oder Junction"):
                                    load_project_context(directory)
                                opened.assert_not_called()

    def test_effective_data_uses_only_current_rules_and_decisions_in_any_order(self):
        data = project_context()
        before = copy.deepcopy(data)
        effective = effective_project_context(data)
        self.assertEqual([record["text"] for record in effective["rules"]], ["Kein Import."])
        self.assertEqual([record["value"] for record in effective["decisions"]], ["Kleine Funktion bauen."])
        self.assertEqual(effective["evidence"], data["evidence"])
        self.assertEqual(effective["tasks"], data["tasks"])
        data["rules"].reverse()
        data["decisions"].reverse()
        self.assertEqual(effective_project_context(data), effective)
        self.assertEqual(sorted(data["rules"], key=lambda item: item["revision"]), before["rules"])
        effective["rules"][0]["text"] = "Mutierter Auszug"
        self.assertEqual(data["rules"][0]["text"], "Kein Import.")

    def test_digest_is_canonical_and_changes_for_history_as_well_as_current_text(self):
        data = project_context()
        digest = project_context_digest(data)
        self.assertEqual(len(digest), 64)
        self.assertEqual(project_context_digest(dict(reversed(list(data.items())))), digest)
        for target in ("rules", "evidence"):
            changed = copy.deepcopy(data)
            changed[target][0]["text"] = "Geändert."
            self.assertNotEqual(project_context_digest(changed), digest)
        invalid = copy.deepcopy(data)
        invalid["trusted"] = True
        with self.assertRaises(ValueError):
            project_context_digest(invalid)


if __name__ == "__main__":
    unittest.main()
