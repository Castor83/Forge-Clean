"""Independent acceptance of automatic, reviewed CLI session continuation."""

import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from src.forge_runtime import session_automation as sessions
from src.forge_runtime.conversation_cli import interact
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.project_session import update_project
from src.forge_runtime.project_control import remove_project_record
from src.forge_runtime.tool_loop import ToolLoopDeadlineExceeded
from tests.test_forge_session_handoff import CHECK_NAMES, state_fixture


ROOT = Path(__file__).resolve().parents[1]
INCOMING = "  Korrigiere die Entscheidung: Unicode ä und alte Regel entfernen.  "


def review_fixture():
    return {"checks": {name: {"status": "pass", "note": "Originalquellen vollständig geprüft."}
                       for name in CHECK_NAMES}, "findings": [],
            "limitations": ["Synthetischer Prüfer, keine echte lokale Modellprobe."]}


class SessionAutomationTests(unittest.TestCase):
    def test_missing_envelope_path_is_controlled_failure(self):
        self.rotate()
        value = json.loads(self.task.data["messages"][0]["content"])
        del value["packet_path"]
        self.task.data["messages"][0]["content"] = json.dumps(value)
        with self.assertRaises(ValueError):
            sessions.validate_session(self.task)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspaces")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = TaskStore(self.root)
        self.lock = self.store.exclusive()
        self.lock.__enter__()
        self.addCleanup(self.lock.__exit__, None, None, None)
        self.task = ConversationTask(self.store)
        self.task.change(messages=[{"role": "user", "content": "Altstand vollständig " + "a" * 8100},
                                   {"role": "assistant", "content": "Bisherige Entscheidung " + "b" * 8100}])
        self.calls = []

    def drafter(self, context):
        self.calls.append(("draft", context))
        return json.dumps(state_fixture(), ensure_ascii=False)

    def reviewer(self, context):
        self.calls.append(("review", context))
        return json.dumps(review_fixture(), ensure_ascii=False)

    def rotate(self, **overrides):
        return sessions.rotate_session(self.task, INCOMING,
                                       overrides.get("drafter", self.drafter),
                                       overrides.get("reviewer", self.reviewer), emit=lambda _: None)

    def files_containing(self, text):
        return [path for path in self.root.rglob("*") if path.is_file()
                and text in path.read_text(encoding="utf-8", errors="replace")]

    def test_trigger_boundary_commands_oversize_and_unsafe_states(self):
        self.task.change(messages=[{"role": "user", "content": "x" * 15999}])
        self.assertFalse(sessions.should_rotate(self.task, ""))
        self.assertTrue(sessions.should_rotate(self.task, "y"))
        self.assertFalse(sessions.should_rotate(self.task, "/status"))
        self.assertFalse(sessions.should_rotate(self.task, "x" * 8001))
        self.task.change(messages=[{"role": "user", "content": "x" * 15998}])
        self.assertFalse(sessions.should_rotate(self.task, "y"))
        self.task.data["state"] = "running"
        self.assertFalse(sessions.should_rotate(self.task, "yyy"))
        self.task.data["state"] = "clarifying"
        self.store.retain_lock_on_close = True
        self.assertFalse(sessions.should_rotate(self.task, "yyy"))
        self.store.retain_lock_on_close = False

    def test_reviewed_rotation_retains_archive_and_revokes_confirmation(self):
        history = copy.deepcopy(self.task.data["messages"])
        self.task.change(state="proposed", proposal={"objective": "old"})
        self.task.confirm()
        result = self.rotate()
        self.assertEqual([call[0] for call in self.calls], ["draft", "review"])
        self.assertEqual(result["pending_path"].read_bytes(), INCOMING.encode("utf-8"))
        draft_sources = json.loads(self.calls[0][1][-1][1])["sources"]
        review_sources = json.loads(self.calls[1][1][-1][1].split("\nENTWURF:\n", 1)[0])["sources"]
        self.assertEqual(draft_sources, review_sources)
        for source in draft_sources:
            self.assertEqual((self.root / source["path"]).read_bytes(), source["content"].encode("utf-8"))
        self.assertEqual(self.task.data["state"], "clarifying")
        self.assertIsNone(self.task.data["proposal"])
        self.assertIsNone(self.task.data["report"])
        self.assertIsNone(self.task.data["confirmed_proposal_sha256"])
        self.assertFalse(self.task.is_confirmed())
        self.assertEqual(len(self.task.data["messages"]), 1)
        self.assertEqual(self.task.data["messages"][0]["role"], "assistant")
        self.assertTrue(self.files_containing(history[0]["content"]))
        self.assertTrue(self.files_containing(history[1]["content"]))
        self.assertTrue(self.files_containing(INCOMING))
        sessions.validate_session(self.task)
        reopened = ConversationTask(self.store)
        sessions.validate_session(reopened)
        self.assertEqual(reopened.data, self.task.data)

    def test_rejected_review_preserves_history_message_and_no_confirmation(self):
        old = copy.deepcopy(self.task.data["messages"])
        self.task.change(state="proposed", proposal={"objective": "old"})
        self.task.confirm()
        rejected = review_fixture()
        rejected["checks"]["source_consistency"]["status"] = "fail"
        with self.assertRaises((ValueError, RuntimeError)):
            self.rotate(reviewer=lambda _: json.dumps(rejected))
        self.assertEqual(self.task.data["messages"], old)
        self.assertFalse(self.task.is_confirmed())
        self.assertTrue(self.files_containing(INCOMING))

    def test_missing_check_is_not_automatically_passed(self):
        incomplete = review_fixture()
        incomplete["checks"].pop("limitations")
        with self.assertRaises((ValueError, RuntimeError)):
            self.rotate(reviewer=lambda _: json.dumps(incomplete))
        self.assertEqual(self.task.data["messages"][0]["role"], "user")
        self.assertTrue(self.files_containing(INCOMING))

    def test_duplicate_model_json_is_rejected_and_review_not_called(self):
        with self.assertRaises((ValueError, RuntimeError)):
            self.rotate(drafter=lambda _: '{"goal":"one","goal":"two"}')
        self.assertEqual(self.calls, [])
        self.assertTrue(self.files_containing(INCOMING))

    def test_overlarge_model_answer_preserves_pending_and_history(self):
        old = copy.deepcopy(self.task.data["messages"])
        with self.assertRaises((ValueError, RuntimeError)):
            self.rotate(drafter=lambda _: "x" * 500001)
        self.assertEqual(self.task.data["messages"], old)
        self.assertTrue(self.files_containing(INCOMING))

    def test_activation_save_failure_keeps_old_history_and_pending(self):
        old = copy.deepcopy(self.task.data["messages"])
        original = self.task.change

        def fail_activation(**fields):
            if fields.get("messages") and fields["messages"][0]["role"] == "assistant":
                raise OSError("synthetic disk failure during activation")
            return original(**fields)

        with patch.object(self.task, "change", side_effect=fail_activation):
            with self.assertRaises(OSError):
                self.rotate()
        self.assertEqual(self.task.data["messages"], old)
        self.assertTrue(self.files_containing(INCOMING))

    def test_models_cannot_change_sources_after_receiving_payload(self):
        for phase in ("draft", "review"):
            with self.subTest(phase=phase):
                def mutate(context):
                    payload = json.loads(context[-1][1].split("\nENTWURF:\n", 1)[0])
                    source = payload["sources"][0]
                    (self.root / source["path"]).write_text(source["content"] + " ", encoding="utf-8")
                    return json.dumps(state_fixture() if phase == "draft" else review_fixture())

                with self.assertRaises((ValueError, RuntimeError)):
                    self.rotate(**{("drafter" if phase == "draft" else "reviewer"): mutate})
                self.assertEqual(self.task.data["messages"][0]["role"], "user")

    def test_mutated_pending_recovers_original_at_exact_reported_path(self):
        for phase in ("draft", "review"):
            with self.subTest(phase=phase):
                events = []
                modified = []

                def mutate(context):
                    payload = json.loads(context[-1][1].split("\nENTWURF:\n", 1)[0])
                    source = next(item for item in payload["sources"]
                                  if item["path"].endswith("/pending.txt"))
                    path = self.root / source["path"]
                    path.write_bytes(b"OTHER")
                    modified.append(path)
                    return json.dumps(state_fixture() if phase == "draft" else review_fixture())

                with self.assertRaises(ValueError):
                    sessions.rotate_session(self.task, INCOMING,
                                            mutate if phase == "draft" else self.drafter,
                                            mutate if phase == "review" else self.reviewer,
                                            emit=events.append)
                announcement = [item for item in events if "Neue Nachricht erhalten unter: " in item]
                self.assertEqual(len(announcement), 1)
                retained = Path(announcement[0].split("Neue Nachricht erhalten unter: ", 1)[1])
                self.assertTrue(retained.name.startswith("pending-recovery-"))
                self.assertEqual(retained.read_bytes(), INCOMING.encode("utf-8"))
                self.assertEqual(modified[0].read_bytes(), b"OTHER")
                self.assertNotEqual(retained, modified[0])
                self.assertEqual(self.task.data["messages"][0]["role"], "user")

    def test_archive_oversize_revokes_old_ready_confirmation(self):
        self.task.change(messages=[{"role": "user", "content": "x" * 100001}],
                         state="proposed", proposal={"objective": "old"})
        self.task.confirm()
        with self.assertRaises(ValueError):
            self.rotate()
        self.assertFalse(self.task.is_confirmed())
        self.assertTrue(self.files_containing(INCOMING))
        self.assertEqual(self.calls, [])

    def test_short_real_deadline_preserves_pending_without_activation(self):
        original = sessions._call_chat_with_deadline

        def fast_deadline(chat, messages, **options):
            options["timeout_seconds"] = .01
            return original(chat, messages, **options)

        def slow(_):
            time.sleep(.05)
            return json.dumps(state_fixture())

        with patch.object(sessions, "_call_chat_with_deadline", side_effect=fast_deadline):
            with self.assertRaises(ToolLoopDeadlineExceeded):
                self.rotate(drafter=slow)
        self.assertEqual(self.task.data["messages"][0]["role"], "user")
        self.assertTrue(self.files_containing(INCOMING))

    def test_activation_readback_failure_retains_lock(self):
        with patch.object(self.store, "load", side_effect=OSError("readback failure")):
            with self.assertRaises(OSError):
                self.rotate()
        self.assertTrue(self.store.retain_lock_on_close)
        self.assertTrue(self.files_containing(INCOMING))
        self.store.retain_lock_on_close = False

    def test_pending_write_failure_does_not_claim_retained_message(self):
        self.task.change(state="proposed", proposal={"objective": "old"})
        self.task.confirm()
        events = []
        with patch.object(sessions, "_write", side_effect=OSError("pending disk failure")):
            with self.assertRaises(OSError):
                sessions.rotate_session(self.task, INCOMING, self.drafter, self.reviewer, emit=events.append)
        self.assertFalse(any("Neue Nachricht erhalten" in event for event in events))
        self.assertTrue(any("unklar" in event for event in events))
        self.assertFalse(self.files_containing(INCOMING))
        self.assertTrue(self.store.retain_lock_on_close)
        self.assertFalse(self.task.is_confirmed())
        self.store.retain_lock_on_close = False

    def test_revocation_save_failure_keeps_pending_durable_and_lock(self):
        self.task.change(state="proposed", proposal={"objective": "old"})
        self.task.confirm()
        with patch.object(self.store, "save", side_effect=OSError("revocation disk failure")):
            with self.assertRaises(OSError):
                self.rotate()
        pending = list(self.root.glob("session-*/pending.txt"))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].read_bytes(), INCOMING.encode("utf-8"))
        self.assertTrue(self.store.retain_lock_on_close)
        self.assertFalse(self.task.is_confirmed())
        self.assertEqual(self.calls, [])
        self.store.retain_lock_on_close = False

    def test_complete_envelope_removal_role_change_and_missing_marker_fail(self):
        self.rotate()
        original = copy.deepcopy(self.task.data)
        variants = [([{ "role": "assistant", "content": "Ordinary replacement"}], original["events"]),
                    ([dict(original["messages"][0], role="user")], original["events"]),
                    ([], original["events"]),
                    (original["messages"], [event for event in original["events"]
                                            if not event.startswith(sessions.ACTIVE_PREFIX)])]
        for messages, events in variants:
            self.task.data = dict(original, messages=messages, events=events)
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                sessions.validate_session(self.task)
        self.task.data = original

    def test_project_initialize_update_remove_after_rotation_remain_bound(self):
        self.rotate()
        self.task.initialize_project("automation-project")
        sessions.validate_session(self.task)
        update_project(self.task, "rules", "old-rule", "Alte Regel")
        update_project(self.task, "decisions", "working_notes", "ASCII")
        self.rotate()
        update_project(self.task, "decisions", "working_notes", "Unicode")
        sessions.validate_session(self.task)
        remove_project_record(self.task, "rules", "old-rule")
        self.assertEqual(self.task.data["messages"], [])
        sessions.validate_session(self.task)
        context = self.task.project_data()
        self.assertFalse(any(record["id"] == "old-rule" for record in context["rules"]))
        self.assertTrue(any(record["key"] == "working_notes" and record["value"] == "Unicode"
                            and record["status"] == "active" for record in context["decisions"]))
        sessions.validate_session(ConversationTask(self.store))

    def test_envelope_tampering_is_rejected_after_reopen(self):
        self.rotate()
        messages = copy.deepcopy(self.task.data["messages"])
        messages[0]["content"] += "\nUngeprüfte zusätzliche Freigabe."
        self.task.change(messages=messages)
        with self.assertRaises((ValueError, RuntimeError)):
            sessions.validate_session(self.task)
        with self.assertRaises((ValueError, RuntimeError)):
            sessions.validate_session(ConversationTask(self.store))

    def test_bound_packet_and_source_tampering_are_rejected(self):
        self.rotate()
        envelope = json.loads(self.task.data["messages"][0]["content"])
        packet = self.root / envelope["packet_path"]
        saved = packet.read_bytes()
        packet.write_bytes(saved + b" ")
        with self.assertRaises((ValueError, RuntimeError)):
            sessions.validate_session(self.task)
        packet.write_bytes(saved)
        package = json.loads(saved)
        archive = next(record for record in package["draft"]["sources"]
                       if record["path"].endswith("/archive.json"))
        (self.root / archive["path"]).write_text("{}", encoding="utf-8")
        with self.assertRaises((ValueError, RuntimeError)):
            sessions.validate_session(self.task)

    def test_two_rotations_keep_current_decision_and_complete_previous_archive(self):
        self.rotate()
        first_envelope = self.task.data["messages"][0]["content"]
        revised = state_fixture()
        revised["rules"] = ["Aktuelle Regel; die entfernte alte Regel gilt nicht mehr."]
        revised["decisions"] = ["Korrigierte Entscheidung: Unicode zulassen."]
        self.task.change(messages=self.task.data["messages"] +
                         [{"role": "user", "content": "Korrektur\n" + "x" * 15000}])
        self.rotate(drafter=lambda _: json.dumps(revised, ensure_ascii=False))
        second = json.loads(self.task.data["messages"][0]["content"])
        self.assertEqual(second["state"]["rules"], revised["rules"])
        self.assertEqual(second["state"]["decisions"], revised["decisions"])
        archives = list(self.root.glob("session-*/archive.json"))
        self.assertEqual(len(archives), 2)
        self.assertTrue(any(first_envelope in [message["content"] for message in
                                              json.loads(path.read_text(encoding="utf-8"))["messages"]]
                            for path in archives))
        sessions.validate_session(ConversationTask(self.store))

    def test_cli_warns_before_models_and_delivers_incoming_once_verbatim(self):
        events = []
        inputs = iter([INCOMING, "/ende"])

        def draft(context):
            self.assertTrue(any("Zeichenheuristik" in str(event) for event in events))
            events.append("draft")
            return self.drafter(context)

        def planner(context):
            events.append("planner")
            self.assertEqual(context[-1], ("user", INCOMING))
            self.assertEqual(sum(content == INCOMING for role, content in context if role == "user"), 1)
            return json.dumps({"reply": "Wunsch erhalten.", "questions": ["Welcher Textbaustein?"], "task": None})

        result = interact(self.task, planner, lambda _: self.fail("build called"),
                          read=lambda _: next(inputs), emit=events.append,
                          session_drafter=draft, session_reviewer=self.reviewer)
        self.assertEqual(result, 0)
        self.assertLess(events.index("draft"), events.index("planner"))
        self.assertEqual(sum(message["content"] == INCOMING for message in self.task.data["messages"]), 1)
        sessions.validate_session(ConversationTask(self.store))

    def test_cli_without_adapters_retains_incoming_without_planner_call(self):
        inputs = iter([INCOMING, "/ende"])
        old = copy.deepcopy(self.task.data["messages"])
        events = []
        interact(self.task, lambda _: self.fail("planner called without reviewed rotation"),
                 lambda _: self.fail("builder called"), read=lambda _: next(inputs), emit=events.append)
        self.assertEqual(self.task.data["messages"], old)
        self.assertTrue(self.files_containing(INCOMING))
        self.assertTrue(any(str(self.root) in str(event) for event in events))


if __name__ == "__main__":
    unittest.main()
