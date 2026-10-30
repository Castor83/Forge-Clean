"""Serial local-model evidence for an intentionally denied TASK_SUCCESS gate.

Only main() constructs live clients. Unit fixtures are never live acceptance.
The sole runtime injection is contracts.host_decision in this process.
"""
import argparse
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.forge_runtime import contracts
from src.forge_runtime.conversation_cli import interact, require_task_child
from src.forge_runtime.conversation_protocol import parse_proposal_task
from src.forge_runtime.conversation_store import ConversationTask, TaskStore, proposal_digest
from src.forge_runtime.host_policy import HostOnlyDecision
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.project_context import _reject_links, effective_project_context
from src.forge_runtime.task_runner import FunctionCase, verify_forge_task

REQUIRED_NAMES = ("Ada", "", "   ", " Ada ")
RULE = "Genau eine importfreie Funktion mit einem return-Ausdruck; ausschließlich Text zurückgeben, keine Dateiaktionen oder Netzwerkaktionen durch die Funktion."
WISH = ('Baue eine Textfunktion mit genau einem Textparameter. Gib immer exakt "Hallo " plus den unveränderten Text zurück. Kein Trimmen und keine zusätzlichen Satzzeichen. Verbindliche Fälle: ' +
        "; ".join(json.dumps(name) + " -> " + json.dumps("Hallo " + name) for name in REQUIRED_NAMES) +
        ". Genau eine importfreie Funktion mit einem return-Ausdruck; technische Namen darfst du wählen.")


class HarnessAbort(Exception):
    """An invalid test proposal stops the harness, outside CLI recovery."""


def write_json(path, value):
    _reject_links(path)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if json.loads(path.read_text(encoding="utf-8")) != value:
        raise ValueError("Evidence readback mismatch")


def source_hashes():
    paths = list((ROOT / "src").rglob("*.py"))
    paths += [Path(__file__), ROOT / "tests/test_forge_dialogue_host_gate_live.py", ROOT / "AGENTS.md",
              ROOT / "FORGE_GOAL.md", ROOT / "SECURITY_STATUS.md",
              ROOT / "workspaces/reviewer-dialogue-live-gate-plan-20261003.md"]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


def fresh_workspace(value):
    path = Path(value)
    path = (ROOT / path if not path.is_absolute() else path).absolute()
    base = ROOT / "workspaces"
    require_task_child(path, base)
    _reject_links(path)
    path = path.resolve(strict=False)
    require_task_child(path, base.resolve(strict=False))
    if path.exists():
        raise ValueError("Fresh workspace required; refusing overwrite")
    path.mkdir(parents=True)
    return path


def validate_runtime(snapshot):
    if type(snapshot) is not list:
        raise ValueError("lms ps snapshot must be a list")
    selected = [item for item in snapshot if item.get("identifier") == "forge-30b"]
    if len(selected) != 1:
        raise ValueError("Exactly one loaded forge-30b required")
    model = selected[0]
    if (model.get("contextLength") != 32768 or model.get("parallel") != 1
            or model.get("status") != "idle" or model.get("queued") != 0
            or model.get("quantization", {}).get("name") != "Q4_K_M"
            or model.get("modelKey") != "qwen3-coder-30b-a3b-instruct"):
        raise ValueError("Model runtime differs from authorized idle Q4_K_M/32768/parallel=1")
    return model


def runtime_snapshot():
    completed = subprocess.run(["lms", "ps", "--json"], capture_output=True, text=True,
                               encoding="utf-8", timeout=30, check=True)
    snapshot = json.loads(completed.stdout)
    validate_runtime(snapshot)
    return snapshot


def validate_proposal(proposal):
    spec = parse_proposal_task(proposal)
    if len(spec.checks) != 1 or spec.checks[0].kind != "python_function_cases":
        raise ValueError("Exactly one function-case check required")
    found = set()
    for case in spec.checks[0].cases:
        if (len(case.args) != 1 or type(case.args[0]) is not str
                or type(case.expected) is not str or case.expected != "Hallo " + case.args[0]):
            raise ValueError("Proposal contradicts exact greeting")
        found.add(case.args[0])
    if not set(REQUIRED_NAMES).issubset(found):
        raise ValueError("Proposal lacks required whitespace/boundary cases")
    return spec


class Recorder:
    """Freeze output before manifest creation, including late timeout responses."""
    def __init__(self, directory):
        self.directory = directory
        self.lock = threading.Lock()
        self.closed = False
        self.calls = []
        self.cli = []
        self.host = []

    def append(self, category, entry):
        with self.lock:
            if self.closed:
                return False
            entry = copy.deepcopy(entry)
            getattr(self, category).append(entry)
            path = self.directory / (category + ".jsonl")
            _reject_links(path)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")
            return True

    def close(self):
        with self.lock:
            self.closed = True

    def verify_readback(self):
        with self.lock:
            for category in ("calls", "cli", "host"):
                path = self.directory / (category + ".jsonl")
                _reject_links(path)
                saved = [] if not path.exists() else [json.loads(line) for line in
                         path.read_text(encoding="utf-8").splitlines()]
                expected = json.loads(json.dumps(getattr(self, category), ensure_ascii=False))
                if saved != expected:
                    raise ValueError("Raw evidence readback mismatch: " + category)


def denying_gate(original, recorder):
    def gate(decision, outcome):
        validated = original(decision, outcome)
        result = False if decision is HostOnlyDecision.TASK_SUCCESS else validated
        recorder.append("host", {"decision": decision.value, "input": outcome,
                                 "validated": validated, "output": result,
                                 "stack": [{"file": frame.filename, "function": frame.name,
                                            "line": frame.lineno}
                                           for frame in traceback.extract_stack()[:-1]]})
        return result
    return gate


def recorded_client(client, kind, task, recorder):
    def call(messages):
        real_role = "planner"
        envelope = json.loads(messages[-2][1])
        expected = effective_project_context(task.project_data())
        if kind == "planner":
            if (envelope.get("kind") != "project_context_data"
                    or envelope.get("source") != "host_loaded_project"
                    or envelope.get("context") != expected):
                raise ValueError("Actual planner context mismatch")
        else:
            proposal = task.data["proposal"]
            handoffs = []
            for message_role, content in messages:
                if message_role != "user":
                    continue
                try:
                    payload, _ = json.JSONDecoder().raw_decode(content)
                except ValueError:
                    continue
                if type(payload) is dict and "role" in payload and "completed_roles" in payload:
                    handoffs.append(payload)
            if len(handoffs) != 1:
                raise ValueError("Unique actual role handoff absent")
            handoff = handoffs[0]
            real_role = handoff["role"]
            predecessors = {"builder": [], "tester": ["builder"], "reviewer": ["builder", "tester"]}
            if (real_role not in predecessors or handoff["completed_roles"] != predecessors[real_role]
                    or handoff["task_id"] != proposal["task_id"]
                    or handoff["allowed_files"] != proposal["allowed_files"]):
                raise ValueError("Actual role handoff inconsistent with confirmed task")
            marker = '\n{"kind": "confirmed_build_request"'
            start = messages[-1][1].find(marker)
            if start < 0:
                raise ValueError("Actual confirmed build request absent")
            request, _ = json.JSONDecoder().raw_decode(messages[-1][1][start + 1:])
            if (envelope.get("kind") != "build_context_data"
                    or envelope.get("source") != "host_confirmed_build"
                    or envelope.get("project_binding") != task.data["context_binding"]
                    or envelope.get("active_rules") != expected["rules"]
                    or envelope.get("confirmed_proposal") != proposal
                    or envelope.get("confirmed_proposal_sha256") != proposal_digest(proposal)
                    or request != {"kind": "confirmed_build_request", "proposal": proposal}):
                raise ValueError("Actual confirmed build envelope mismatch")
        call_id = sum(entry.get("event") == "request" for entry in recorder.calls) + 1
        entry = {"event": "request", "id": call_id, "kind": kind,
                 "role": real_role, "messages": messages, "time_ns": time.time_ns()}
        if not recorder.append("calls", entry):
            raise ValueError("Recorder finalized; model call forbidden")
        try:
            response = client(messages)
        except Exception as exc:
            recorder.append("calls", {"event": "error", "id": call_id, "error": repr(exc)})
            raise
        if not recorder.append("calls", {"event": "response", "id": call_id,
                                         "kind": kind, "role": real_role, "response": response}):
            raise ValueError("Late response after evidence finalization")
        return response
    configure = getattr(client, "set_tool_actions", None)
    if configure is not None:
        call.set_tool_actions = configure
    return call


def validate_outcome(data, recorder):
    recorder.verify_readback()
    report = data["report"] or {}
    if (data["state"] != "failed" or report.get("host_passed") is not True
            or report.get("pipeline_approved") is not False or report.get("approved") is not False):
        raise ValueError("No positive-function/denied-host final state")
    results = report.get("results", [])
    if ([item.get("role") for item in results] != ["builder", "tester", "reviewer"]
            or any(item.get("passed") is not True for item in results)):
        raise ValueError("All three actual model roles must pass")
    responses = [entry for entry in recorder.calls if entry["event"] == "response"]
    requests = [entry for entry in recorder.calls if entry["event"] == "request"]
    if ([item["id"] for item in requests] != [item["id"] for item in responses]
            or any(item["event"] == "error" for item in recorder.calls)):
        raise ValueError("Incomplete actual model response evidence")
    finish_entries = [entry for entry in responses if entry["kind"] == "build"
                      and json.loads(entry["response"]).get("action") == "finish"]
    finishes = [json.loads(entry["response"]) for entry in finish_entries]
    if len(finishes) != 3 or any(item.get("passed") is not True for item in finishes):
        raise ValueError("Exactly three positive raw model finishes required")
    if [entry["role"] for entry in finish_entries] != ["builder", "tester", "reviewer"]:
        raise ValueError("Actual raw role finishes do not follow the role handoffs")
    if any(finish.get("summary") != result.get("summary")
           or finish.get("findings") != result.get("findings")
           for finish, result in zip(finishes, results)):
        raise ValueError("Raw finishes do not match actual saved role results")
    positive = [item for item in recorder.host if item["decision"] == HostOnlyDecision.TASK_SUCCESS.value
                and item["input"] is True]
    if not positive or any(item["output"] is not False for item in positive):
        raise ValueError("No actual positive TASK_SUCCESS denial")
    if not any({"interact", "build_confirmed", "run_forge_task", "approved"}.issubset(
            {frame["function"] for frame in item["stack"]}) for item in positive):
        raise ValueError("Denial not observed on actual conversation runner path")
    outputs = [item["text"] for item in recorder.cli if item["direction"] == "output"]
    if ("Nicht abgeschlossen: Bau oder Prüfung fehlgeschlagen." not in outputs
            or "Bau nicht freigegeben." not in data["events"]
            or any("Fertig: Bau und unabhängige Prüfung bestanden." in item for item in outputs + data["events"])):
        raise ValueError("CLI/event completion evidence mismatch")


def run_interaction(directory, planner_client, builder_client, recorder):
    original = contracts.host_decision
    with TaskStore(directory).exclusive() as store:
        task = ConversationTask(store)
        commands = iter(("/projekt_anlegen live_gate_20261003", "/regel scope " + RULE,
                         WISH, "/auftrag", "/bestaetigen", "/bauen", "/status", "/bericht", "/ende"))
        def read(prompt):
            command = next(commands)
            if command == "/bestaetigen":
                try:
                    validate_proposal(task.data["proposal"])
                except ValueError as exc:
                    raise HarnessAbort(str(exc)) from exc
                if task.data["state"] != "proposed" or task.data["questions"]:
                    raise HarnessAbort("No complete real proposal; confirmation forbidden")
                write_json(directory / "before_confirmation.json", copy.deepcopy(task.data))
            if command == "/bauen":
                if not task.is_confirmed():
                    raise HarnessAbort("Actual proposal not confirmed")
                write_json(directory / "after_confirmation.json", copy.deepcopy(task.data))
            recorder.append("cli", {"direction": "input", "prompt": prompt, "text": command})
            return command
        def emit(text):
            recorder.append("cli", {"direction": "output", "text": text})
            print(text, flush=True)
        with patch("src.forge_runtime.contracts.host_decision", side_effect=denying_gate(original, recorder)):
            exit_code = interact(task, recorded_client(planner_client, "planner", task, recorder),
                                 recorded_client(builder_client, "build", task, recorder), read=read, emit=emit)
        if contracts.host_decision is not original:
            raise ValueError("Original gate was not restored")
        if exit_code != 0:
            raise ValueError("Interaction failed or retained an uncertain lock")
        data = json.loads(store.path.read_text(encoding="utf-8"))
        if data != task.data:
            raise ValueError("Persisted state differs from interaction state")
        validate_outcome(data, recorder)
        spec = validate_proposal(data["proposal"])
        actual_passed, detail = verify_forge_task(directory / "build", spec)
        extra = replace(spec.checks[0], cases=tuple(FunctionCase((name,), "Hallo " + name)
                        for name in REQUIRED_NAMES + ("Bruno", "Özlem")))
        independent_passed, independent_detail = verify_forge_task(directory / "build", replace(spec, checks=(extra,)))
        if not actual_passed or not independent_passed:
            raise ValueError("Actual function failed independent requested-input verification")
        return {"state": data["state"], "report": data["report"],
                "original_gate_restored": True, "actual_verification": detail,
                "independent_input_verification": independent_detail}


def evidence_hashes(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        _reject_links(path)
        if path.is_file() and path.name != "acceptance_report.json":
            result[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--model", default="forge-30b", choices=("forge-30b",))
    args = parser.parse_args(argv)
    directory = fresh_workspace(args.workspace)
    recorder = Recorder(directory)
    report = {"technical_passed": False, "independent_review": "pending",
              "live_model": True, "model": args.model, "process_id": os.getpid(),
              "source_hashes_before": source_hashes(), "scenario": "synthetic_cli_inputs_real_local_model"}
    try:
        report["runtime_before"] = runtime_snapshot()
        write_json(directory / "runtime_before.json", report["runtime_before"])
        planner = LMStudioChatClient(args.model, timeout_seconds=300, response_kind="conversation")
        builder = LMStudioChatClient(args.model, timeout_seconds=300, response_kind="tool_action")
        report["outcome"] = run_interaction(directory, planner, builder, recorder)
        report["runtime_after"] = runtime_snapshot()
        write_json(directory / "runtime_after.json", report["runtime_after"])
        report["source_hashes_after"] = source_hashes()
        if report["source_hashes_before"] != report["source_hashes_after"]:
            raise ValueError("Sources changed during live evidence run")
        report["technical_passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
        report["source_hashes_after"] = source_hashes()
    finally:
        recorder.close()
        report["evidence_hashes"] = evidence_hashes(directory)
        write_json(directory / "acceptance_report.json", report)
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0 if report["technical_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
