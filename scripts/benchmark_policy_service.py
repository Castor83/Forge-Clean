"""Serial fresh builds with fixed inputs and independent host readback."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

from src.forge_runtime.host_suites import run_host_suite
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.policy_service_suite import CORE_FILES, SERVICE_FILES, DEPENDENCIES, verify_policy_service
from src.forge_runtime.task_runner import parse_task_spec, run_forge_task, format_run_report
from src.forge_runtime.workspace_tools import WorkspaceFileTools


REPOSITORY = Path(__file__).resolve().parents[1]
ORIGINS = {
    "agent/audit.py": "forge-partitioned-durable-20261001",
    "agent/audit_acl.py": "forge-partitioned-durable-20261001",
    "agent/config_analysis_service.py": "forge-audited-config-analysis-20261001",
    "agent/config_analyzer.py": "forge-redacted-config-analysis-fresh",
}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def frozen_inputs():
    inputs = {"SPEC.md": (REPOSITORY / "examples/forge_policy_service_SPEC.md").read_bytes()}
    for path, origin in ORIGINS.items():
        inputs[path] = (REPOSITORY / "workspaces" / origin / path).read_bytes()
    return inputs


def verifier_hashes():
    paths = sorted((REPOSITORY / "src/forge_runtime").glob("*.py"))
    paths += sorted((REPOSITORY / "src/obsidian_policy").glob("*.py"))
    paths += [Path(__file__).resolve(), REPOSITORY / "examples/forge_policy_service.json"]
    return {p.relative_to(REPOSITORY).as_posix(): digest(p.read_bytes()) for p in paths}


class MeasuredChat:
    def __init__(self, client, trace):
        self.client = client
        self.trace = trace
        self.model_calls = 0
        self.builder_starts = 0
        self.repair_actions = 0

    def set_tool_actions(self, actions):
        self.client.set_tool_actions(actions)

    def __call__(self, messages):
        self.model_calls += 1
        handoff = json.loads(messages[1][1])
        if handoff["role"] == "builder" and len(messages) in (2, 3):
            # New normal builder has two messages; repair adds host feedback.
            if len(messages) == 2 or json.loads(messages[2][1]).get("kind") == "host_check_feedback":
                self.builder_starts += 1
        started = time.monotonic()
        record = {"role": handoff["role"], "last_input": messages[-1][1]}
        try:
            response = self.client(messages)
            record["response"] = response
            try:
                action = json.loads(response).get("action")
            except (ValueError, AttributeError):
                action = None
            if action in ("replace_text", "replace_text_batch"):
                self.repair_actions += 1
            print(f"  {handoff['role']}: {action}", flush=True)
            return response
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            with self.trace.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def execute_run(root, spec, inputs, chat):
    root.mkdir()  # Existing output trees are never reused or overwritten.
    for name, data in inputs.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    measured = MeasuredChat(chat, root / "trace.jsonl")
    started = time.monotonic()
    result = {"workspace": root.relative_to(REPOSITORY).as_posix(), "approved": False,
              "independent_passed": False, "manual_interventions": 0}
    try:
        pipeline = run_forge_task(root, spec, measured, max_steps=20,
                                  max_call_seconds=300, max_elapsed_seconds=900,
                                  max_repair_attempts=1)
        result["pipeline"] = json.loads(format_run_report(pipeline))
        result["approved"] = pipeline.approved
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["build_seconds"] = round(time.monotonic() - started, 3)
    result["model_calls"] = measured.model_calls
    result["repair_loops"] = max(0, measured.builder_starts - 1)
    result["repair_actions"] = measured.repair_actions
    result["inputs_unchanged"] = all((root / p).read_bytes() == data for p, data in inputs.items())
    workspace = WorkspaceFileTools(root, spec.task)
    try:
        passed, detail = run_host_suite("policy_service_v1", workspace)
        result["independent_passed"] = passed
        result["independent_detail"] = detail
        if result["approved"] and passed and result["inputs_unchanged"]:
            contents = {p: workspace.read_text(p) for p in DEPENDENCIES}
            for name, parts in (("core", CORE_FILES), ("decision_service", SERVICE_FILES)):
                source = "".join(workspace.read_text(p) for p in parts)
                path = root / f"agent/{name}.py"
                path.write_text(source, encoding="utf-8")
                contents[f"agent/{name}.py"] = path.read_text(encoding="utf-8")
                if contents[f"agent/{name}.py"] != source:
                    raise ValueError("host assembly readback mismatch")
            failure = verify_policy_service(contents)
            result["assembled_passed"] = failure is None
            result["assembly_detail"] = failure
            result["artifact_sha256"] = {p: digest((root / p).read_bytes()) for p in contents}
    except Exception as exc:
        result["independent_passed"] = False
        result["verification_error"] = f"{type(exc).__name__}: {exc}"
    result["success"] = bool(result["approved"] and result["independent_passed"] and result["inputs_unchanged"] and result.get("assembled_passed"))
    result["first_attempt_success"] = result["success"] and measured.builder_starts == 1
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", required=True)
    parser.add_argument("--runs", type=int, choices=(1, 3), default=3)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,40}", args.series):
        raise ValueError("series must be a short lowercase identifier")
    root = REPOSITORY / "workspaces" / f"forge-policy-{args.series}"
    root.mkdir()
    inputs = frozen_inputs()
    hashes = verifier_hashes()
    spec = parse_task_spec((REPOSITORY / "examples/forge_policy_service.json").read_text(encoding="utf-8"))
    report = {"started_utc": datetime.now(timezone.utc).isoformat(), "model": "forge-30b",
              "temperature": 0, "input_sha256": {p: digest(v) for p, v in inputs.items()},
              "verifier_sha256": hashes, "runs": []}
    for index in range(1, args.runs + 1):
        if verifier_hashes() != hashes or frozen_inputs() != inputs:
            raise RuntimeError("frozen benchmark conditions changed")
        print(f"RUN {index}/{args.runs}", flush=True)
        result = execute_run(root / f"run-{index}", spec, inputs,
                             LMStudioChatClient(model="forge-30b", timeout_seconds=300))
        report["runs"].append(result)
        report["conditions_unchanged"] = verifier_hashes() == hashes and frozen_inputs() == inputs
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result), flush=True)
        if not report["conditions_unchanged"]:
            raise RuntimeError("benchmark conditions changed during the run")
    return 0 if all(run["success"] for run in report["runs"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
