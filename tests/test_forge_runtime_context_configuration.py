"""Exercise the actual PowerShell launcher with an isolated, inert CLI fixture."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
MODEL_KEY = "qwen3-coder-30b-a3b-instruct"


def loaded_model(context=32768, identifier="forge-30b", maximum=262144):
    return {"type": "llm", "modelKey": MODEL_KEY, "identifier": identifier,
            "contextLength": context, "maxContextLength": maximum,
            "status": "idle", "queued": 0, "parallel": 1}


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required for launcher behavior tests")
class RuntimeContextConfigurationTests(unittest.TestCase):
    def run_launcher(self, models, context=None, *, running=True, load_exit=0, after_load_models=None):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = directory / "state.json"
            fixture = {"models": models, "running": running, "load_exit": load_exit,
                       "after_load_models": ([loaded_model(context or 32768)]
                                             if after_load_models is None else after_load_models)}
            state.write_text(json.dumps(fixture), encoding="utf-8")
            log = directory / "commands.jsonl"
            mock = directory / "mock_lms.ps1"
            mock.write_text(
                "$fixture = Get-Content -LiteralPath $env:FORGE_CONTEXT_TEST_STATE -Raw | ConvertFrom-Json\n"
                "$entry = ConvertTo-Json -InputObject @($args) -Compress\n"
                "Add-Content -LiteralPath $env:FORGE_CONTEXT_TEST_LOG -Value $entry -Encoding UTF8\n"
                "$global:LASTEXITCODE = 0\n"
                "if ($args[0] -eq 'ps') {\n"
                "    if ($global:ForgeContextMockWasLoaded) {\n"
                "        ConvertTo-Json -InputObject @($global:ForgeContextMockModels) -Depth 8 -Compress\n"
                "    } else { ConvertTo-Json -InputObject @($fixture.models) -Depth 8 -Compress }\n"
                "}\n"
                "elseif ($args[0] -eq 'load') {\n"
                "    $global:LASTEXITCODE = [int]$fixture.load_exit\n"
                "    $global:ForgeContextMockWasLoaded = $true\n"
                "    $global:ForgeContextMockModels = @($fixture.after_load_models)\n"
                "}\n"
                "elseif ($args[0] -eq 'server' -and $args[1] -eq 'status') {\n"
                "    ConvertTo-Json -InputObject @{running=[bool]$fixture.running} -Compress\n"
                "}\n",
                encoding="utf-8",
            )
            # Only the known CLI location is redirected. The unmodified launcher
            # still selects models, constructs commands and branches on status.
            harness = directory / "harness.ps1"
            arguments = " -Action Start" + (" -ContextLength " + str(context) if context is not None else "")
            harness.write_text(
                "$ErrorActionPreference = 'Stop'\n"
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
                "function Join-Path {\n"
                "    param($Path, $ChildPath)\n"
                "    if ($ChildPath -eq '.lmstudio\\bin\\lms.exe') { return $env:FORGE_CONTEXT_TEST_CLI }\n"
                "    Microsoft.PowerShell.Management\\Join-Path -Path $Path -ChildPath $ChildPath\n"
                "}\n"
                "try {\n"
                "    & " + ps_literal(ROOT / "scripts/manage_agent1.ps1") + arguments + "\n"
                "    exit 0\n"
                "}\n"
                "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }\n",
                encoding="utf-8",
            )
            environment = dict(os.environ, FORGE_CONTEXT_TEST_STATE=str(state),
                               FORGE_CONTEXT_TEST_LOG=str(log), FORGE_CONTEXT_TEST_CLI=str(mock))
            completed = subprocess.run(
                [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8",
                timeout=30,
            )
            entries = [json.loads(line) for line in log.read_text(encoding="utf-8-sig").splitlines()] if log.exists() else []
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), fixture)
            return completed, entries

    def test_default_start_loads_32768_context_with_one_parallel_slot(self):
        completed, commands = self.run_launcher([])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(commands, [["ps", "--json"],
            ["load", MODEL_KEY, "--context-length", 32768, "--parallel", 1,
             "--ttl", 3600, "--identifier", "forge-30b", "--yes"],
            ["ps", "--json"],
            ["server", "status", "--json"]])

    def test_explicit_64k_override_reaches_cli_and_does_not_stop_server(self):
        completed, commands = self.run_launcher([], 65536)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        load = commands[1]
        self.assertEqual(load[load.index("--context-length") + 1], 65536)
        self.assertEqual([command[0] for command in commands], ["ps", "load", "ps", "server"])
        self.assertIn("65536", completed.stdout)

    def test_matching_loaded_context_is_reused_without_loading_or_unloading(self):
        completed, commands = self.run_launcher([loaded_model(65536)], 65536)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(commands, [["ps", "--json"], ["server", "status", "--json"]])

    def test_context_mismatch_fails_before_server_actions_and_names_exact_reload(self):
        completed, commands = self.run_launcher([loaded_model(32768)], 65536)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(commands, [["ps", "--json"]])
        self.assertIn("32768", completed.stderr)
        self.assertIn("65536", completed.stderr)
        self.assertIn("unload 'forge-30b'", completed.stderr)
        self.assertIn("-Action Start -ContextLength 65536", completed.stderr)

    def test_foreign_identifier_is_not_unloaded_or_told_to_be_unloaded(self):
        completed, commands = self.run_launcher([loaded_model(32768, identifier="user_owned_30b")], 65536)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(commands, [["ps", "--json"]])
        self.assertNotIn("unload 'user_owned_30b'", completed.stderr)
        self.assertIn("user_owned_30b", completed.stderr)

    def test_missing_boolean_or_fractional_context_metadata_cannot_be_reused(self):
        variants = []
        missing = loaded_model()
        del missing["contextLength"]
        variants.append(missing)
        for context in (True, 32768.5, 0):
            variants.append(loaded_model(context))
        for value in variants:
            with self.subTest(value=value):
                completed, commands = self.run_launcher([value])
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(commands, [["ps", "--json"]])
                self.assertIn("contextLength", completed.stderr)

    def test_model_maximum_is_enforced_without_changing_runtime(self):
        completed, commands = self.run_launcher([loaded_model(maximum=65536)], 131072)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(commands, [["ps", "--json"]])
        self.assertIn("Modellmaximum 65536", completed.stderr)

    def test_wrong_model_under_forge_identifier_is_not_adopted(self):
        model = loaded_model()
        model["modelKey"] = "different/model"
        completed, commands = self.run_launcher([model])
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(commands, [["ps", "--json"]])

    def test_loaded_model_without_identifier_cannot_report_readiness(self):
        completed, commands = self.run_launcher([loaded_model(identifier="")])
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(commands, [["ps", "--json"]])
        self.assertIn("Kennung", completed.stderr)

    def test_out_of_range_context_fails_before_any_cli_call(self):
        for context in (0, 262145):
            with self.subTest(context=context):
                completed, commands = self.run_launcher([], context)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(commands, [])

    def test_failed_model_load_does_not_start_server(self):
        completed, commands = self.run_launcher([], 65536, running=False, load_exit=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual([command[0] for command in commands], ["ps", "load"])

    def test_successful_cli_load_with_wrong_actual_context_is_not_accepted(self):
        completed, commands = self.run_launcher([], 65536, after_load_models=[loaded_model(32768)])
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual([command[0] for command in commands], ["ps", "load", "ps"])
        self.assertIn("32768", completed.stderr)

    def test_successful_cli_load_without_own_instance_is_not_accepted(self):
        for actual in ([], [loaded_model(65536, identifier="foreign_model")]):
            with self.subTest(actual=actual):
                completed, commands = self.run_launcher([], 65536, after_load_models=actual)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual([command[0] for command in commands], ["ps", "load", "ps"])

    def test_post_load_wrong_model_and_missing_or_wrong_metadata_block_server(self):
        variants = []
        wrong_model = loaded_model(65536)
        wrong_model["modelKey"] = "different/model"
        variants.append(wrong_model)
        missing = loaded_model(65536)
        del missing["maxContextLength"]
        variants.append(missing)
        for parallel in (True, 2, "1"):
            wrong_parallel = loaded_model(65536)
            wrong_parallel["parallel"] = parallel
            variants.append(wrong_parallel)
        for actual in variants:
            with self.subTest(actual=actual):
                completed, commands = self.run_launcher([], 65536, after_load_models=[actual])
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual([command[0] for command in commands], ["ps", "load", "ps"])

    def test_existing_wrong_parallel_configuration_is_not_reused(self):
        model = loaded_model()
        model["parallel"] = 2
        completed, commands = self.run_launcher([model])
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(commands, [["ps", "--json"]])
        self.assertIn("parallel=1", completed.stderr)

    def test_existing_array_model_metadata_cannot_be_reused(self):
        for field in ("type", "modelKey"):
            with self.subTest(field=field):
                model = loaded_model()
                model[field] = [model[field]]
                completed, commands = self.run_launcher([model])
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(commands, [["ps", "--json"]])

    def test_post_load_array_model_metadata_blocks_server(self):
        for field in ("type", "modelKey"):
            with self.subTest(field=field):
                model = loaded_model()
                model[field] = [model[field]]
                completed, commands = self.run_launcher([], after_load_models=[model])
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual([command[0] for command in commands], ["ps", "load", "ps"])

    def test_existing_start_behavior_keeps_loopback_server_start(self):
        completed, commands = self.run_launcher([loaded_model()], running=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(commands, [["ps", "--json"], ["server", "status", "--json"],
                                   ["server", "start", "--port", 1234, "--bind", "127.0.0.1"]])


if __name__ == "__main__":
    unittest.main()
