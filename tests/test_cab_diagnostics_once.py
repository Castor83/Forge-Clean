"""All external boundaries are fake; guards, counters and host evaluation are real."""
from dataclasses import replace
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts import cab_diagnostics_offline as offline
try:
    from scripts import cab_diagnostics_once as once
except ImportError:
    once = None

ROOT = Path(__file__).resolve().parents[1]
GOOD = ("def should_retry(error_kind, already_retried):\n"
        "    return isinstance(error_kind, str) and error_kind.strip() == "
        "'temporary' and already_retried is False\n")
RUNTIME = [{"identifier": "forge-30b", "contextLength": 32768, "parallel": 1,
            "status": "idle", "queued": 0, "quantization": {"name": "Q4_K_M"},
            "modelKey": "qwen3-coder-30b-a3b-instruct"}]


def envelope(content):
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


class TestCabOnce(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(once, "CAB once wrapper missing")
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspaces", prefix="cab-once-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.preparation = self.root / "prepared"
        self.prepared = offline.prepare_inputs(self.preparation)
        self.contract_path = self.root / "new-contract.json"
        self.contract = once.current_contract(self.preparation)
        self.write_contract()
        self.calls = []
        self.runtime_calls = 0
        self.samples = []
        self.responses = [envelope(json.loads(self.prepared.historical_c_response)
                                   ["choices"][0]["message"]["content"]),
                          envelope(GOOD), envelope(json.dumps({
                              "action": "replace_text_batch", "path": offline.OUTPUT,
                              "replacements": [{"old": self.prepared.endcode.decode(), "new": GOOD}]}))]

    def write_contract(self):
        self.contract_path.write_bytes(json.dumps(self.contract, sort_keys=True).encode())
        self.pin = hashlib.sha256(self.contract_path.read_bytes()).hexdigest()

    def runtime(self):
        self.runtime_calls += 1
        return json.loads(json.dumps(RUNTIME))

    def collect(self, started):
        sample = {"utc": "fixture", "elapsed_seconds": time.monotonic() - started,
                  "system_ram": None, "gpus": [], "errors": []}
        self.samples.append(sample)
        return sample

    def summarize(self, samples, stop_reason, interval):
        return {"sample_count": len(samples), "stop_reason": stop_reason,
                "interval_seconds": interval, "fixture": True}

    def transport(self, raw, directory):
        number = len(self.calls)
        counter = json.loads((directory.parent / "counter.json").read_bytes())
        self.assertEqual(counter["attempts"], number + 1)
        self.assertEqual(counter["order"], list("CAB")[:number + 1])
        self.calls.append(raw)
        return once.HTTPResult(self.responses[number], {"status": 200, "body_complete": True})

    def run_once(self, **changes):
        values = dict(transport=self.transport, runtime=self.runtime,
                      collect_sample=self.collect, summarize=self.summarize)
        values.update(changes)
        return once.run_once(self.root / "run", self.preparation,
                             self.contract_path, self.pin, **values)

    def test_three_serial_calls_counter_readback_raw_bytes_and_real_hostchecks(self):
        report = self.run_once()
        self.assertEqual(self.calls, [self.prepared.c_request, self.prepared.a_request,
                                     self.prepared.b_request])
        self.assertEqual(report["attempts"], 3)
        self.assertEqual([report["conditions"][c]["status"] for c in "AB"], ["PASS", "PASS"])
        self.assertTrue(report["sources_unchanged"])
        self.assertEqual(report["conditions"]["C"]["category"], "historical_action_reproduced")
        self.assertFalse((self.root / "run/C/agent/retry_decision.py").exists())
        for condition, response in zip("CAB", self.responses):
            self.assertEqual((self.root / f"run/{condition}/http-response.raw").read_bytes(), response)
        self.assertEqual(json.loads((self.root / "run/counter.json").read_bytes())["attempts"], 3)
        self.assertGreaterEqual(self.runtime_calls, 4)

    def test_a_format_fail_and_empty_content_allow_b_exactly_once(self):
        for content in ("", "```python\n" + GOOD + "```", "x" * 20001):
            with self.subTest(content=content[:20]):
                self.responses[1] = envelope(content)
                target = self.root / ("run-" + str(len(content)))
                report = once.run_once(target, self.preparation, self.contract_path, self.pin,
                                       transport=lambda raw, directory: once.HTTPResult(
                                           self.responses[list("CAB").index(directory.name)],
                                           {"status": 200, "body_complete": True}),
                                       runtime=self.runtime, collect_sample=self.collect,
                                       summarize=self.summarize)
                self.assertEqual(report["attempts"], 3)
                self.assertEqual(report["conditions"]["A"]["status"], "FAIL")
                self.assertEqual(report["conditions"]["B"]["status"], "PASS")

    def test_null_missing_invalid_json_or_invalid_utf8_content_stop_without_retry(self):
        for number, raw in enumerate((envelope(None), b'{"choices":[{"message":{}}]}',
                                      b'{', b'\xff', envelope("\ud800"))):
            with self.subTest(raw=raw[:25]):
                calls = []
                def bad_transport(body, directory):
                    calls.append(body)
                    return once.HTTPResult(raw, {"status": 200, "body_complete": True})
                report = once.run_once(self.root / f"invalid-{number}", self.preparation,
                                       self.contract_path, self.pin, transport=bad_transport,
                                       runtime=self.runtime, collect_sample=self.collect,
                                       summarize=self.summarize)
                self.assertEqual(len(calls), 1)
                self.assertEqual(report["conditions"]["A"]["status"], "UNGETESTET")
                self.assertEqual(report["conditions"]["B"]["status"], "UNGETESTET")
                self.assertTrue(report["error"])

    def test_missing_or_wrong_pin_blocks_before_all_external_boundaries(self):
        for pin in (None, "", "a" * 64, offline.FREEZE_HASH):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                once.run_once(self.root / "run", self.preparation, self.contract_path, pin,
                              transport=self.transport, runtime=self.runtime,
                              collect_sample=self.collect, summarize=self.summarize)
        self.assertEqual((self.calls, self.runtime_calls, self.samples), ([], 0, []))

    def test_partial_offline_injections_are_rejected_before_defaults_or_output(self):
        values = dict(transport=self.transport, runtime=self.runtime,
                      collect_sample=self.collect, summarize=self.summarize)
        with patch.object(once, "direct_http", side_effect=AssertionError("live transport selected")), \
             patch.object(once, "runtime_snapshot", side_effect=AssertionError("live runtime selected")), \
             patch.object(once, "_monitor_functions", side_effect=AssertionError("live monitor selected")):
            for missing in values:
                target = self.root / ("partial-" + missing)
                options = dict(values)
                options[missing] = None
                with self.subTest(missing=missing), self.assertRaises(ValueError):
                    once.run_once(target, self.preparation, self.contract_path, self.pin, **options)
                self.assertFalse(target.exists())
        self.assertEqual((self.calls, self.runtime_calls, self.samples), ([], 0, []))

    def test_parent_traversal_is_rejected_before_any_filesystem_boundary_check(self):
        paths = (ROOT / "workspaces/../tests/cab-escape",
                 ROOT / "workspaces/../../cab-escape",
                 self.root / "nested/../normalized")
        for path in paths:
            with self.subTest(path=str(path)), \
                 patch.object(once, "_reject_links", side_effect=AssertionError("filesystem access before traversal rejection")), \
                 self.assertRaises(ValueError):
                once._repo_path(path)

    def test_complete_task_configuration_inventory_and_extra_binding_drift_are_rejected(self):
        for field in ("task", "configuration", "sources", "additional_bindings", "cab_configuration", "git_head"):
            with self.subTest(field=field):
                original = self.contract[field]
                self.contract[field] = {} if isinstance(original, dict) else "changed"
                self.write_contract()
                with self.assertRaises(ValueError):
                    self.run_once()
                self.contract[field] = original
        self.assertEqual((self.calls, self.runtime_calls), ([], 0))

    def test_payload_drift_before_start_blocks_and_between_calls_stops(self):
        path = self.preparation / "request-A.json"
        def drift_transport(raw, directory):
            result = self.transport(raw, directory)
            path.write_bytes(path.read_bytes() + b" ")
            return result
        report = self.run_once(transport=drift_transport)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report["conditions"]["A"]["status"], "UNGETESTET")
        self.assertFalse(report["sources_unchanged"])
        with self.assertRaises(ValueError):
            self.run_once()

    def test_new_or_removed_source_inventory_entry_is_not_filtered(self):
        sources = self.contract["sources"]
        with patch.object(once.benchmark, "source_hashes", return_value=sources | {"src/new.py": "a" * 64}):
            with self.assertRaises(ValueError):
                once.guard_contract(self.contract_path, self.pin, self.preparation)
        with patch.object(once.benchmark, "source_hashes", return_value=dict(list(sources.items())[1:])):
            with self.assertRaises(ValueError):
                once.guard_contract(self.contract_path, self.pin, self.preparation)

    def test_runtime_drift_stops_remaining_conditions(self):
        def runtime():
            result = self.runtime()
            if self.runtime_calls > 1:
                result[0]["queued"] = 1
            return result
        report = self.run_once(runtime=runtime)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report["conditions"]["A"]["status"], "UNGETESTET")

    def test_transport_exception_http_redirect_incomplete_or_oversize_stop_and_archive(self):
        variants = (OSError("fixture transport abort"),
                    once.HTTPResult(envelope(""), {"status": 302, "body_complete": True}),
                    once.HTTPResult(b'partial', {"status": 200, "body_complete": False}),
                    once.HTTPResult(b'x' * 1000001, {"status": 200, "body_complete": True}))
        for number, result in enumerate(variants):
            calls = []
            def transport(raw, directory):
                calls.append(raw)
                if isinstance(result, Exception):
                    raise result
                return result
            report = once.run_once(self.root / f"bad-http-{number}", self.preparation,
                                   self.contract_path, self.pin, transport=transport,
                                   runtime=self.runtime, collect_sample=self.collect,
                                   summarize=self.summarize)
            self.assertEqual(len(calls), 1)
            self.assertEqual(report["attempts"], 1)
            self.assertEqual(report["conditions"]["A"]["status"], "UNGETESTET")
            self.assertTrue((self.root / f"bad-http-{number}/C/http-metadata.json").is_file())

    def test_run_root_is_exclusive_no_resume_even_after_abort(self):
        report = self.run_once(transport=lambda *args: (_ for _ in ()).throw(OSError("abort")))
        self.assertEqual(report["attempts"], 1)
        with self.assertRaises((ValueError, FileExistsError)):
            self.run_once()
        self.assertEqual(self.calls, [])

    def test_competing_start_cannot_contact_same_root(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def blocking(raw, directory):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.transport(raw, directory)
        def worker():
            try:
                self.run_once(transport=blocking)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises((ValueError, FileExistsError)):
                self.run_once()
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.calls), 3)

    def test_monitor_records_before_during_after_and_phase_boundaries(self):
        report = self.run_once()
        records = [json.loads(line) for line in (self.root / "run/resources.jsonl").read_text().splitlines()]
        phases = json.loads((self.root / "run/resource-phases.json").read_bytes())
        self.assertEqual([phase["name"] for phase in phases],
                         ["before_calls", "C_begin", "C_end", "A_begin", "A_end", "B_begin", "B_end", "after_calls"])
        self.assertTrue({"before_calls", "C_begin", "A_begin", "B_begin", "after_calls"}
                        .issubset({sample["phase"] for sample in records}))
        self.assertEqual(report["resources"]["sample_count"], len(records))
        self.assertTrue(report["resources"]["fixture"])

    def test_free_reserve_uses_numeric_samples_only_and_keeps_machine_gpu_scope(self):
        count = [0]
        def collect(started):
            count[0] += 1
            value = self.collect(started)
            ram = (None, True, 800, 300, "N/A", 500)[count[0] % 6]
            value["system_ram"] = {"physical_available_bytes": ram, "physical_used_bytes": 1000}
            value["gpus"] = [{"index": 0, "memory_free_mib": 100 if count[0] == 4 else None}]
            return value
        report = self.run_once(collect_sample=collect)
        resources = report["resources"]
        self.assertEqual(resources["system_physical_available_bytes"]["observed_sample_min"], 300)
        self.assertEqual(resources["gpu_free_mib_minimum_by_index"], {"0": 100})
        self.assertIn("whole_machine", resources["measurement_scope"])
        self.assertFalse(resources["model_isolated_usage"])


class FakeSocket:
    def __init__(self):
        self.shutdowns = []
    def shutdown(self, how):
        self.shutdowns.append(how)


class FakeResponse:
    status = 200
    length = 0
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.closed = False
    def getheaders(self):
        return [("Content-Type", "application/json")]
    def getheader(self, key):
        return None
    def isclosed(self):
        return self.closed
    def read1(self, size):
        if not self.chunks:
            self.closed = True
            return b""
        return self.chunks.pop(0)


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.sock = FakeSocket()
        self.calls = []
        self.closed = False
    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
    def getresponse(self):
        return self.response
    def close(self):
        self.closed = True


class TestCabDirectHTTP(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(once, "CAB once wrapper missing")
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspaces")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_fixed_direct_post_rawbytes_no_redirect_and_readback(self):
        connection = FakeConnection(FakeResponse((b'{}',)))
        seen = []
        def factory(host, port, timeout):
            seen.append((host, port, timeout))
            return connection
        result = once.direct_http(b'raw C bytes', self.directory, connection_factory=factory)
        self.assertEqual(seen, [("127.0.0.1", 1234, 300)])
        self.assertEqual(connection.calls, [(("POST", "/v1/chat/completions"),
                                          {"body": b'raw C bytes', "headers": {"Content-Type": "application/json"}})])
        self.assertTrue(result.metadata["body_complete"])
        self.assertEqual(result.raw, b'{}')
        self.assertTrue(connection.closed)

    def test_continuous_chunks_cannot_extend_absolute_monotone_deadline(self):
        clock = [0.0]
        class SlowChunks(FakeResponse):
            def read1(self, size):
                clock[0] += 101
                return b'x'
        connection = FakeConnection(SlowChunks())
        result = once.direct_http(b'{}', self.directory,
                                  connection_factory=lambda *args, **kwargs: connection,
                                  monotonic=lambda: clock[0])
        self.assertFalse(result.metadata["body_complete"])
        self.assertTrue(result.metadata["deadline_exceeded"])
        self.assertLessEqual(len(result.raw), 3)
        self.assertTrue(connection.sock.shutdowns)

    def test_active_deadline_aborts_blocked_body_and_preserves_partial_evidence(self):
        connection = FakeConnection(None)
        class Blocking(FakeResponse):
            def read1(self, size):
                while not connection.sock.shutdowns:
                    time.sleep(.001)
                raise OSError('socket shutdown by deadline')
        connection.response = Blocking()
        requested = []
        def timer(seconds, callback):
            requested.append(seconds)
            return threading.Timer(.02, callback)
        result = once.direct_http(b'{}', self.directory,
                                  connection_factory=lambda *args, **kwargs: connection,
                                  timer_factory=timer)
        self.assertEqual(requested, [300])
        self.assertTrue(result.metadata["deadline_exceeded"])
        self.assertFalse(result.metadata["body_complete"])
        self.assertTrue((self.directory / "http-response.raw").exists())
        self.assertTrue((self.directory / "http-metadata.json").exists())

    def test_incomplete_read_archives_partial_and_does_not_retry(self):
        class Incomplete(FakeResponse):
            def read1(self, size):
                raise http.client.IncompleteRead(b'partial')
        connection = FakeConnection(Incomplete())
        result = once.direct_http(b'{}', self.directory,
                                  connection_factory=lambda *args, **kwargs: connection)
        self.assertEqual(result.raw, b'partial')
        self.assertFalse(result.metadata["body_complete"])
        self.assertEqual(len(connection.calls), 1)

    def test_deadline_expiry_during_constructor_stops_before_request(self):
        connection = FakeConnection(FakeResponse())
        callback = []
        class Timer:
            def __init__(self, seconds, action):
                callback.append(action)
            def start(self):
                pass
            def cancel(self):
                pass
        def factory(*args, **kwargs):
            callback[0]()
            return connection
        result = once.direct_http(b'{}', self.directory,
                                  connection_factory=factory, timer_factory=Timer)
        self.assertEqual(connection.calls, [])
        self.assertTrue(result.metadata["deadline_exceeded"])

    def test_connected_socket_preserves_abort_handle_after_http_detaches_it(self):
        connection = FakeConnection(None)
        active = connection.sock
        connection._active_socket = active
        class Detached(FakeResponse):
            def read1(self, size):
                connection.sock = None
                while not active.shutdowns:
                    time.sleep(.001)
                raise OSError('active socket shutdown')
        connection.response = Detached()
        result = once.direct_http(b'{}', self.directory,
                                  connection_factory=lambda *args, **kwargs: connection,
                                  timer_factory=lambda seconds, callback: threading.Timer(.02, callback))
        self.assertFalse(result.metadata["body_complete"])
        self.assertTrue(active.shutdowns)

    def test_abortable_connection_checks_cancel_before_any_socket_connect(self):
        expired = threading.Event()
        expired.set()
        connection = once._AbortableConnection("127.0.0.1", 1234, timeout=300)
        connection._cab_expired = expired
        connection._cab_deadline = time.monotonic() + 300
        connection._cab_monotonic = time.monotonic
        with patch("scripts.cab_diagnostics_once.socket.socket", side_effect=AssertionError("socket creation after abort")):
            with self.assertRaises(TimeoutError):
                connection.connect()


if __name__ == "__main__":
    unittest.main()
