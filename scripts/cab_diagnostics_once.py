"""Fixed, externally pinned C/A/B diagnosis. Import and preparation are offline."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import benchmark_retry_build as benchmark
from scripts import cab_diagnostics_offline as offline
from scripts.verify_dialogue_host_gate import runtime_snapshot, validate_runtime
from src.forge_runtime.project_context import _reject_links

MONITOR_PATH = ROOT / "workspaces/monitor-iq4xs-resources-20261008.py"
RUN = ROOT / "workspaces/forge-cab-once-20261013"
PREPARATION = ROOT / "workspaces/cab-offline-prepared-20261013"
CAB_CONFIG = {"order": ["C", "A", "B"], "max_calls": 3, "retries": 0,
              "fallbacks": 0, "resume": False, "call_seconds": 300,
              "raw_response_limit_bytes": 1000000, "max_file_chars": 20000,
              "endpoint": "http://127.0.0.1:1234/v1/chat/completions",
              "transport": "direct_http_no_proxy_no_redirect_no_retry",
              "resource_interval_seconds": 1,
              "run_directory": "workspaces/forge-cab-once-20261013"}
EXTRA_FILES = (
    "scripts/cab_diagnostics_once.py", "tests/test_cab_diagnostics_once.py",
    "scripts/cab_diagnostics_offline.py", "tests/test_cab_diagnostics_offline.py",
    "scripts/manage_agent1.ps1",
    "docs/superpowers/specs/2026-10-13-cab-once-design.md",
    "docs/superpowers/plans/2026-10-13-cab-once.md",
    "workspaces/monitor-iq4xs-resources-20261008.py",
    "workspaces/cab-model-preflight-prepared-20261013.md",
    "workspaces/retry-final-diagnostics-freeze-20261006.json",
    "workspaces/forge-retry-final-diagnostics-20261006/request-021.json",
    "workspaces/forge-retry-final-diagnostics-20261006/agent/retry_decision.py",
    "workspaces/forge-retry-final-diagnostics-20261006/http-response-021.json",
    "workspaces/forge-retry-final-diagnostics-20261006/http-metadata-021.json",
)
PREPARED_FILES = ("request-C.json", "request-A.json", "request-B.json",
                  "input-manifest.json", "historical-C-http-response.json")


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _repo_path(value):
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("parent traversal is forbidden before filesystem access")
    path = path.absolute()
    if not path.is_relative_to(ROOT):
        raise ValueError("path must stay inside this repository")
    _reject_links(path)
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(ROOT):
        raise ValueError("resolved path must stay inside this repository")
    return resolved


def _write_bytes(path, raw, *, exclusive=False):
    _reject_links(path)
    with path.open("xb" if exclusive else "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != raw:
        raise ValueError("evidence readback mismatch: " + path.name)


def _save(path, value):
    raw = _json_bytes(value)
    temporary = path.with_name(path.name + ".tmp")
    _write_bytes(temporary, raw, exclusive=True)
    _reject_links(path)
    os.replace(temporary, path)
    if path.read_bytes() != raw:
        raise ValueError("atomic evidence readback mismatch: " + path.name)


def current_contract(preparation_directory):
    """Return a review candidate, never an automatically accepted external pin."""
    preparation = _repo_path(preparation_directory)
    if not preparation.is_relative_to(ROOT / "workspaces"):
        raise ValueError("preparation must be below workspaces")
    paths = [ROOT / name for name in EXTRA_FILES]
    paths += [preparation / name for name in PREPARED_FILES]
    for path in [*paths, *(ROOT / "src").rglob("*.py")]:
        _repo_path(path)
    current = benchmark.current_state()
    original = (ROOT / "workspaces/retry-final-diagnostics-freeze-20261006.json").read_bytes()
    if _digest(original) != offline.FREEZE_HASH:
        raise ValueError("historical freeze changed; never rebind it")
    if offline._strict_json(original)["task"] != current["task"]:
        raise ValueError("complete task differs from historical contract")
    manifest = offline._strict_json((preparation / "input-manifest.json").read_bytes())
    if manifest.get("binding_status") != "offline_prepared_not_final_source_binding":
        raise ValueError("prepared inputs are not the offline candidate")
    for condition in "CAB":
        raw = (preparation / f"request-{condition}.json").read_bytes()
        entry = manifest["payloads"][condition]
        if entry != {"path": f"request-{condition}.json", "sha256": _digest(raw), "bytes": len(raw)}:
            raise ValueError("prepared payload differs from its manifest")
        payload = offline._strict_json(raw)
        if (payload.get("model") != "forge-30b" or type(payload.get("temperature")) is not int
                or payload["temperature"] != 0 or payload.get("stream") is not False):
            raise ValueError("fixed request model/temperature/stream changed")
        if condition == "C" and _digest(raw) != offline.C_HASH:
            raise ValueError("historical C request is not byte identical")
        if condition in "AB":
            if offline._strict_json(payload["messages"][1]["content"])["task"] != current["task"]:
                raise ValueError("A/B request lacks the complete unchanged task")
    current.update(kind="cab_once_sources_v1", cab_configuration=dict(CAB_CONFIG),
                   additional_bindings={path.relative_to(ROOT).as_posix(): _digest(path.read_bytes())
                                        for path in sorted(paths)},
                   git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                                    text=True).strip())
    return current


def guard_contract(contract_path, external_pin, preparation_directory):
    if (type(external_pin) is not str or re.fullmatch(r"[0-9a-f]{64}", external_pin) is None
            or external_pin == offline.FREEZE_HASH):
        raise ValueError("a separately confirmed NEW external SHA256 pin is required")
    path = _repo_path(contract_path)
    raw = path.read_bytes()
    if _digest(raw) != external_pin:
        raise ValueError("externally pinned CAB contract changed")
    frozen = offline._strict_json(raw)
    if frozen != current_contract(preparation_directory):
        raise ValueError("complete CAB task/configuration/source inventory/bindings changed")
    return frozen


@dataclass(frozen=True)
class HTTPResult:
    raw: bytes
    metadata: dict


class _AbortableConnection(http.client.HTTPConnection):
    """Expose the fixed socket even while response.makefile owns its descriptor."""
    def connect(self):
        if self._cab_expired.is_set() or self._cab_monotonic() >= self._cab_deadline:
            raise TimeoutError("connection cancelled before socket creation")
        self._active_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock = self._active_socket
        if self._cab_expired.is_set() or self._cab_monotonic() >= self._cab_deadline:
            self.close()
            raise TimeoutError("connection cancelled before connect")
        self.sock.settimeout(max(.001, self._cab_deadline - self._cab_monotonic()))
        self.sock.connect(("127.0.0.1", 1234))


def direct_http(body, directory, *, connection_factory=_AbortableConnection,
                timer_factory=threading.Timer, monotonic=time.monotonic):
    """One direct POST, active shutdown plus an absolute deadline, never retry."""
    directory = _repo_path(directory)
    started = monotonic()
    deadline = started + 300
    expired = threading.Event()
    connection = None
    response = None
    raw = bytearray()
    metadata = {"status": None, "headers": [], "body_complete": False,
                "read_error": None, "deadline_exceeded": False, "overflow_observed": False}

    def abort():
        expired.set()
        if connection is not None:
            active = getattr(connection, "_active_socket", None) or getattr(connection, "sock", None)
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

    def check_deadline():
        if expired.is_set() or monotonic() >= deadline:
            abort()
            raise TimeoutError("absolute 300-second monotone deadline exceeded")

    timer = timer_factory(300, abort)
    timer.daemon = True
    timer.start()
    try:
        check_deadline()
        connection = connection_factory("127.0.0.1", 1234, timeout=300)
        connection._cab_expired = expired
        connection._cab_deadline = deadline
        connection._cab_monotonic = monotonic
        check_deadline()
        connection.request("POST", "/v1/chat/completions", body=body,
                           headers={"Content-Type": "application/json"})
        check_deadline()
        response = connection.getresponse()
        metadata.update(status=response.status, headers=response.getheaders(),
                        content_length=response.getheader("Content-Length"))
        check_deadline()
        while not response.isclosed():
            check_deadline()
            chunk = response.read1(min(65536, 1000001 - len(raw)))
            raw.extend(chunk)
            _write_bytes(directory / "http-response.raw", bytes(raw[:1000000]))
            check_deadline()
            if len(raw) > 1000000:
                metadata["overflow_observed"] = True
                raise ValueError("raw response exceeds 1000000-byte budget")
            if not chunk:
                break
        declared = metadata["content_length"]
        metadata["body_complete"] = bool(
            response.isclosed() and response.length in (None, 0)
            and (declared is None or (declared.isdigit() and int(declared) == len(raw))))
        check_deadline()
    except http.client.IncompleteRead as exc:
        raw.extend(exc.partial)
        metadata["overflow_observed"] = len(raw) > 1000000
        metadata["read_error"] = type(exc).__name__ + ": " + str(exc)
    except Exception as exc:
        metadata["read_error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        timer.cancel()
        if connection is not None:
            connection.close()
        metadata.update(deadline_exceeded=expired.is_set() or monotonic() >= deadline,
                        elapsed_seconds=monotonic() - started, observed_response_bytes=len(raw),
                        body_sha256=_digest(bytes(raw[:1000000])))
        if metadata["deadline_exceeded"] or metadata["read_error"] or metadata["overflow_observed"]:
            metadata["body_complete"] = False
        _write_bytes(directory / "http-response.raw", bytes(raw[:1000000]))
        _save(directory / "http-metadata.json", metadata)
    return HTTPResult(bytes(raw[:1000000]), metadata)


def _monitor_functions():
    spec = importlib.util.spec_from_file_location("cab_bound_resource_monitor", MONITOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.collect_sample, module.summarize


def _numeric_stats(values):
    values = [value for value in values if type(value) is int
              or (type(value) is float and math.isfinite(value))]
    result = {"numeric_samples": len(values)}
    if values:
        result.update(observed_sample_min=min(values), observed_sample_max=max(values))
    return result


class _PassiveMonitor:
    def __init__(self, directory, collect, summarize):
        self.directory, self.collect, self.summarize = directory, collect, summarize
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.samples, self.phases = [], []
        self.phase = "before_calls"
        self.error = None
        self.thread = threading.Thread(target=self._poll, daemon=True)

    def _sample(self):
        with self.lock:
            value = self.collect(self.started)
            if type(value) is not dict:
                raise ValueError("monitor sample must be a dictionary")
            value = dict(value, phase=self.phase)
            raw = _json_bytes(value)
            with (self.directory / "resources.jsonl").open("ab") as stream:
                stream.write(json.dumps(value, ensure_ascii=True, allow_nan=False).encode() + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.samples.append(json.loads(raw))

    def _poll(self):
        try:
            while not self.stop.wait(1):
                self._sample()
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
            self.stop.set()

    def mark(self, name):
        if self.error:
            raise RuntimeError("passive monitor failed: " + self.error)
        with self.lock:
            self.phase = name
            self.phases.append({"name": name, "utc": datetime.now(timezone.utc).isoformat(),
                                "monotonic_seconds": time.monotonic()})
            _save(self.directory / "resource-phases.json", self.phases)
        self._sample()

    def start(self):
        self.mark("before_calls")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(30)
            if self.thread.is_alive():
                raise RuntimeError("passive monitor did not stop")
        self.mark("after_calls")
        summary = self.summarize(self.samples, "cab_finished" if self.error is None else self.error, 1)
        if type(summary) is not dict:
            raise ValueError("monitor summary must be a dictionary")
        summary = dict(summary)
        summary["system_physical_available_bytes"] = _numeric_stats(
            [(sample.get("system_ram") or {}).get("physical_available_bytes") for sample in self.samples])
        gpu_free = {}
        for sample in self.samples:
            for gpu in sample.get("gpus", []):
                gpu_free.setdefault(str(gpu["index"]), []).append(gpu.get("memory_free_mib"))
        summary["gpu_free_mib_minimum_by_index"] = {
            index: stats["observed_sample_min"] for index, values in gpu_free.items()
            if (stats := _numeric_stats(values))["numeric_samples"]}
        summary.update(measurement_scope="whole_machine_and_gpu; separate_named_process_metrics",
                       model_isolated_usage=False)
        _save(self.directory / "resources-summary.json", summary)
        return summary


def _content(raw):
    envelope = offline._strict_json(raw)
    try:
        content = envelope["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("technical response envelope lacks content") from exc
    if type(content) is not str:
        raise ValueError("technical response content must be a string")
    content.encode("utf-8", errors="strict")
    return content


def _compare_c(content, historical_raw):
    try:
        current = offline._strict_json(content)
        old = offline._strict_json(_content(historical_raw))
        if current == old:
            category = "historical_action_reproduced"
        elif (type(current) is dict and type(current.get("replacements")) is list
              and any(type(item) is dict and type(item.get("old")) is str
                      and item.get("old") == item.get("new") for item in current["replacements"])):
            category = "other_old_equals_new"
        else:
            category = "other_response_pattern"
    except (ValueError, TypeError):
        category = "format_mismatch"
    return {"condition": "C", "status": "OBSERVED", "category": category,
            "host_passed": None, "action_applied": False}


def run_once(run_directory, preparation_directory, contract_path, external_pin, *,
             transport=None, runtime=None, collect_sample=None, summarize=None):
    """Offline injections are explicit; the CLI supplies none and cannot change budgets."""
    injected = (transport, runtime, collect_sample, summarize)
    offline_injected = any(value is not None for value in injected)
    if offline_injected and any(value is None for value in injected):
        raise ValueError("offline mode requires transport, runtime, collect_sample and summarize together")
    frozen = guard_contract(contract_path, external_pin, preparation_directory)
    directory = _repo_path(run_directory)
    if not directory.is_relative_to(ROOT / "workspaces") or directory == ROOT / "workspaces":
        raise ValueError("fresh run root must be a child of workspaces")
    if not offline_injected and directory != RUN:
        raise ValueError("live entry has exactly one fixed run root")
    directory.mkdir(parents=True, exist_ok=False)
    report = {"kind": "cab_once_result_v1", "execution_mode": "offline_injected" if offline_injected else "live",
              "external_pin": external_pin, "contract": frozen, "attempts": 0,
              "conditions": {condition: {"status": "UNGETESTET"} for condition in "CAB"},
              "sources_unchanged": False, "error": None,
              "model_loading_performed": False, "loading_measurements": "not_performed_by_wrapper",
              "model_file_identity_verification": "not_performed_by_wrapper",
              "chat_template_verification": "not_performed_by_wrapper"}
    counter = {"attempts": 0, "order": [], "closed": False}
    _save(directory / "counter.json", counter)
    _save(directory / "result.json", report)
    transport = direct_http if transport is None else transport
    runtime = runtime_snapshot if runtime is None else runtime
    monitor = None
    contacted = False
    current_condition = None
    try:
        if collect_sample is None or summarize is None:
            if offline_injected:
                raise ValueError("offline tests must supply both passive monitor functions")
            collect_sample, summarize = _monitor_functions()
        monitor = _PassiveMonitor(directory, collect_sample, summarize)
        monitor.start()
        prepared = offline.prepare_inputs(directory / "input-copy")
        bodies = {"C": prepared.c_request, "A": prepared.a_request, "B": prepared.b_request}
        for condition in "CAB":
            current_condition, contacted = condition, False
            guard_contract(contract_path, external_pin, preparation_directory)
            snapshot = runtime()
            _save(directory / f"runtime-{condition}-before.json", snapshot)
            validate_runtime(snapshot)
            monitor.mark(condition + "_begin")
            raw = (Path(preparation_directory) / f"request-{condition}.json").read_bytes()
            if raw != bodies[condition]:
                raise ValueError("bound request differs from independently prepared bytes")
            call_directory = directory / condition
            call_directory.mkdir()
            _write_bytes(call_directory / "request.json", raw, exclusive=True)
            guard_contract(contract_path, external_pin, preparation_directory)
            counter["attempts"] += 1
            counter["order"].append(condition)
            _save(directory / "counter.json", counter)
            report["attempts"] = counter["attempts"]
            contacted = True
            try:
                response = transport(raw, call_directory)
            except Exception as exc:
                if not (call_directory / "http-response.raw").exists():
                    _write_bytes(call_directory / "http-response.raw", b"", exclusive=True)
                if not (call_directory / "http-metadata.json").exists():
                    _save(call_directory / "http-metadata.json", {"status": None, "body_complete": False,
                                                                "error": type(exc).__name__ + ": " + str(exc)})
                raise
            if type(response) is not HTTPResult or type(response.raw) is not bytes or type(response.metadata) is not dict:
                raise ValueError("transport returned an invalid raw evidence result")
            _write_bytes(call_directory / "http-response.raw", response.raw[:1000000])
            _save(call_directory / "http-metadata.json", response.metadata)
            if (len(response.raw) > 1000000 or response.metadata.get("status") != 200
                    or response.metadata.get("body_complete") is not True
                    or response.metadata.get("deadline_exceeded") is True
                    or response.metadata.get("overflow_observed") is True):
                raise ValueError("technical HTTP status/completeness/deadline/budget failure; no retry")
            content = _content(response.raw)
            _write_bytes(call_directory / "content.txt", content.encode("utf-8"), exclusive=True)
            if condition == "C":
                result = _compare_c(content, prepared.historical_c_response)
            elif condition == "A":
                result = offline.evaluate_a(content, directory / "A-evaluation")
            else:
                result = offline.evaluate_b(content, directory / "B-evaluation")
            report["conditions"][condition] = result
            _save(call_directory / "condition-result.json", result)
            monitor.mark(condition + "_end")
            _save(directory / "result.json", report)
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        if contacted:
            report["conditions"][current_condition] = {"status": "ABGEBROCHEN", "category": "technical",
                                                        "detail": report["error"]}
    finally:
        try:
            guard_contract(contract_path, external_pin, preparation_directory)
            report["sources_unchanged"] = True
        except Exception as exc:
            report["final_source_error"] = type(exc).__name__ + ": " + str(exc)
            report["error"] = report["error"] or report["final_source_error"]
        try:
            snapshot = runtime()
            _save(directory / "runtime-after.json", snapshot)
            validate_runtime(snapshot)
            report["runtime_after_valid"] = True
        except Exception as exc:
            report["runtime_after_valid"] = False
            report["runtime_after_error"] = type(exc).__name__ + ": " + str(exc)
            report["error"] = report["error"] or report["runtime_after_error"]
        if monitor is not None:
            try:
                report["resources"] = monitor.close()
            except Exception as exc:
                report["monitor_error"] = type(exc).__name__ + ": " + str(exc)
                report["error"] = report["error"] or report["monitor_error"]
        counter["closed"] = True
        _save(directory / "counter.json", counter)
        report["completed"] = (report["attempts"] == 3 and not report["error"])
        _save(directory / "result.json", report)
    return offline._strict_json((directory / "result.json").read_bytes())


def main():
    parser = argparse.ArgumentParser(description="One externally pinned C/A/B run; no retry or resume")
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--external-pin", required=True)
    args = parser.parse_args()
    report = run_once(RUN, PREPARATION, args.contract, args.external_pin)
    print(json.dumps(report, ensure_ascii=True, allow_nan=False))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
