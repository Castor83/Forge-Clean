"""Invalid task locations must not cause a provider load as a side effect."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.forge_runtime import ollama_cli
from src.forge_runtime.ollama_runtime import OllamaRuntime
from test_forge_ollama_runtime import ProviderTransport


class OllamaCliBoundaryTests(unittest.TestCase):
    def test_invalid_windows_chat_workspace_names_do_not_contact_model(self):
        repository = Path(ollama_cli.__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repository / "workspaces") as directory:
            for spelling in ("bad?name", "NUL", "trailing."):
                with self.subTest(spelling=spelling):
                    transport = ProviderTransport()
                    runtime = OllamaRuntime(opener=transport)
                    with patch.object(ollama_cli, "OllamaRuntime", return_value=runtime), \
                            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(ollama_cli.main([
                            "--action", "chat", "--workspace", str(Path(directory) / spelling),
                        ]), 1)
                    self.assertEqual(transport.requests, [])

    def test_existing_file_or_file_parent_is_rejected_before_model_contact(self):
        repository = Path(ollama_cli.__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repository / "workspaces") as directory:
            file_path = Path(directory) / "regular-file"
            file_path.write_text("Preserve this user file.", encoding="utf-8")
            for workspace in (file_path, file_path / "child"):
                with self.subTest(workspace=workspace):
                    transport = ProviderTransport()
                    runtime = OllamaRuntime(opener=transport)
                    with patch.object(ollama_cli, "OllamaRuntime", return_value=runtime), \
                            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(ollama_cli.main([
                            "--action", "chat", "--workspace", str(workspace),
                        ]), 1)
                    self.assertEqual(transport.requests, [])
            self.assertEqual(file_path.read_text(encoding="utf-8"), "Preserve this user file.")

    def test_outside_chat_workspace_is_rejected_before_model_contact(self):
        transport = ProviderTransport()
        runtime = OllamaRuntime(opener=transport)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(ollama_cli, "OllamaRuntime", return_value=runtime), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(ollama_cli.main(["--action", "chat", "--workspace", directory]), 1)
        self.assertEqual(transport.requests, [])

    def test_invalid_build_task_is_rejected_before_model_contact(self):
        transport = ProviderTransport()
        runtime = OllamaRuntime(opener=transport)
        repository = Path(ollama_cli.__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repository / "workspaces") as directory, \
                patch.object(ollama_cli, "OllamaRuntime", return_value=runtime), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            task = Path(directory) / "invalid-task.json"
            task.write_text('{"not_a_forge_task":true}', encoding="utf-8")
            self.assertEqual(ollama_cli.main(["--action", "run", "--workspace", directory,
                                            "--task-file", str(task)]), 1)
        self.assertEqual(transport.requests, [])

    def test_unsafe_writable_or_read_only_paths_are_rejected_before_model_contact(self):
        repository = Path(ollama_cli.__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repository / "workspaces") as directory:
            task_file = Path(directory) / "task.json"
            for field, path in (("allowed_files", "../escape.py"),
                                ("allowed_files", "NUL.py"),
                                ("read_only_files", "../private.txt")):
                with self.subTest(field=field, path=path):
                    spec = {"task_id": "invalid-path-test", "objective": "Do not contact a model.",
                            "allowed_files": ["agent.py"],
                            "checks": [{"kind": "python_syntax", "path": "agent.py"}]}
                    spec[field] = [path]
                    if field == "allowed_files":
                        spec["checks"][0]["path"] = path
                    task_file.write_text(json.dumps(spec), encoding="utf-8")
                    transport = ProviderTransport()
                    runtime = OllamaRuntime(opener=transport)
                    with patch.object(ollama_cli, "OllamaRuntime", return_value=runtime), \
                            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(ollama_cli.main([
                            "--action", "run", "--workspace", directory,
                            "--task-file", str(task_file),
                        ]), 1)
                    self.assertEqual(transport.requests, [])


if __name__ == "__main__":
    unittest.main()
