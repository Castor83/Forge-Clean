"""Current exact model identity through the real runtime gate and launcher."""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_dialogue_host_gate import validate_runtime
from tests import test_forge_runtime_context_configuration as launcher_tests


ROOT = Path(__file__).resolve().parents[1]


def current_model():
    return {"identifier": "forge-30b", "type": "llm",
            "modelKey": "qwen3-coder-30b-a3b-instruct", "contextLength": 32768,
            "maxContextLength": 262144, "parallel": 1, "status": "idle",
            "queued": 0, "quantization": {"name": "Q4_K_M"}}


class CurrentModelRuntimeIdentityTests(unittest.TestCase):
    def test_current_catalog_key_is_accepted_without_rewriting_snapshot(self):
        model = current_model()
        before = deepcopy(model)
        try:
            accepted = validate_runtime([model])
        except ValueError as error:
            self.fail("Current catalog identity was rejected: " + str(error))
        self.assertIs(accepted, model)
        self.assertEqual(model, before)

    def test_historical_alias_is_rejected_even_with_all_other_valid_fields(self):
        model = current_model()
        model["modelKey"] = "qwen/qwen3-coder-30b"
        with self.assertRaises(ValueError):
            validate_runtime([model])

    def test_similar_names_and_non_string_keys_are_not_aliases(self):
        for key in ("Qwen3-coder-30b-a3b-instruct", "qwen3-coder-30b-a3b-instruct ",
                    "other/qwen3-coder-30b-a3b-instruct", "qwen3-coder-30b",
                    None, ["qwen3-coder-30b-a3b-instruct"]):
            with self.subTest(key=key):
                model = current_model()
                model["modelKey"] = key
                with self.assertRaises(ValueError):
                    validate_runtime([model])

    def test_current_key_preserves_all_existing_runtime_conditions(self):
        mutations = (("identifier", "user-owned"), ("contextLength", 32767),
                     ("parallel", 2), ("status", "busy"), ("queued", 1),
                     ("quantization", {"name": "Q5_K_M"}))
        for field, value in mutations:
            with self.subTest(field=field):
                model = current_model()
                model[field] = value
                with self.assertRaises(ValueError):
                    validate_runtime([model])
        with self.assertRaises(ValueError):
            validate_runtime([current_model(), current_model()])


@unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
class CurrentModelLauncherIdentityTests(unittest.TestCase):
    def launch(self, models, *, after_load=None):
        helper = launcher_tests.RuntimeContextConfigurationTests(
            "test_default_start_loads_32768_context_with_one_parallel_slot")
        real_temporary_directory = tempfile.TemporaryDirectory

        def repository_temporary_directory(*args, **kwargs):
            kwargs["dir"] = ROOT / "workspaces"
            return real_temporary_directory(*args, **kwargs)

        # Only fixture storage and the external CLI are replaced. The real
        # PowerShell launcher still selects, checks and constructs every call.
        with patch.object(launcher_tests.tempfile, "TemporaryDirectory",
                          repository_temporary_directory):
            return helper.run_launcher(models, after_load_models=(
                [current_model()] if after_load is None else after_load))

    def test_start_loads_current_key_once_with_unchanged_parameters(self):
        completed, commands = self.launch([])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(commands, [["ps", "--json"],
            ["load", "qwen3-coder-30b-a3b-instruct", "--context-length", 32768,
             "--parallel", 1, "--ttl", 3600, "--identifier", "forge-30b", "--yes"],
            ["ps", "--json"], ["server", "status", "--json"]])

    def test_current_loaded_model_is_reused_without_load_or_unload(self):
        completed, commands = self.launch([current_model()])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(commands, [["ps", "--json"], ["server", "status", "--json"]])

    def test_old_loaded_alias_stops_before_loading_or_server_contact(self):
        model = current_model()
        model["modelKey"] = "qwen/qwen3-coder-30b"
        completed, commands = self.launch([model])
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(commands, [["ps", "--json"]])

    def test_old_alias_returned_after_load_cannot_start_server(self):
        model = current_model()
        model["modelKey"] = "qwen/qwen3-coder-30b"
        completed, commands = self.launch([], after_load=[model])
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual([command[0] for command in commands], ["ps", "load", "ps"])

    def test_current_key_with_wrong_context_stops_before_server_contact(self):
        model = current_model()
        model["contextLength"] = 8192
        completed, commands = self.launch([model])
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(commands, [["ps", "--json"]])


if __name__ == "__main__":
    unittest.main()
