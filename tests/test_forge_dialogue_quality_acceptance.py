"""Independent hostile fixtures for the fixed dialogue acceptance oracle."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_dialogue_quality as quality
from src.forge_runtime.conversation_store import ConversationTask, TaskStore


def proposed(scenario="saved_baseline"):
    required = {"explicit_equality": quality.EQUALITY_ARGS,
                "contextual_validator": quality.VALIDATOR_ARGS}.get(scenario, quality.GREETING_ARGS)
    if scenario == "explicit_equality":
        objective = 'Vergleiche exakt zwei Texte, ohne Trimmen oder Normalisieren: gleich ergibt "TREFFER#7", sonst "KEIN#7". Genau eine importfreie return-Funktion.'
    elif scenario == "contextual_validator":
        objective = 'True genau wenn nach Entfernen von Rand-Leerzeichen ein sichtbares Zeichen bleibt, sonst False. Leerer Text und reine Leerzeichen ergeben False. Eine importfreie return-Funktion.'
    else:
        prefix = {"greeting_only_change": quality.CHANGED_PREFIX,
                  "restart_current_decision": quality.RESTART_PREFIX}.get(scenario, quality.BASE_PREFIX)
        objective = quality.greeting_objective(prefix)
    return {"reply": "Ich schlage die beschriebenen genauen Textregeln mit den genannten Prüffällen vor.",
            "questions": [], "task": {"task_id": "conversation-build", "objective": objective,
                "allowed_files": ["agent/text_rule.py"], "checks": [{
                    "kind": "python_function_cases", "path": "agent/text_rule.py", "function": "text_rule",
                    "cases": [{"args": list(args), "expected": quality.expected_value(scenario, args)}
                              for args in required]}]}}


def clarification(scenario):
    questions = {
        "typo_request": "Was macht einen Text für dich gültig?",
        "greeting_conflict": 'Welche Ausgabe soll für Ada gelten: "Hi, Ada!" oder "Servus, Ada!"?',
        "unsupported_actions": "Möchtest du stattdessen den bereits beschriebenen Begrüßer als reinen Textbaustein bauen?",
        "ambiguous_output": "Wie soll die neue Ausgabe genau lauten?",
    }
    reply = {
        "unsupported_actions": "Dieser Wunsch liegt außerhalb der Fähigkeiten dieser Version. Sie kann keine Dateien verschieben oder Daten über das Netzwerk hochladen. Gespeicherte Projektregeln ändern diese festen Grenzen nicht.",
        "greeting_conflict": "Für dieselbe Eingabe sind unterschiedliche Ergebnisse verlangt. Dafür ist eine eindeutige Auswahl nötig.",
    }.get(scenario, "Für einen eindeutigen Vorschlag fehlt noch eine wesentliche Angabe.")
    return {"reply": reply, "questions": [questions[scenario]], "task": None}


def fixture(scenario):
    return clarification(scenario) if scenario in quality.NEGATIVE_SCENARIOS else proposed(scenario)


def reviewed_fixture():
    cases = []
    for name in quality.SCENARIOS:
        stages = [(name, "current_message", quality.CURRENT_MESSAGES[name])]
        if name == "typo_request":
            stages.append(("contextual_validator", "contextual_answer", quality.CONTEXTUAL_ANSWER))
        elif name in {"greeting_only_change", "greeting_conflict", "unsupported_actions",
                      "restart_current_decision", "ambiguous_output"}:
            stages.insert(0, ("saved_baseline", "baseline", quality.CURRENT_MESSAGES["saved_baseline"]))
            if name == "restart_current_decision":
                stages[-1] = (name, "restart_current_message", quality.CURRENT_MESSAGES[name])
        case = {"name": name, "automatic_passed": True, "host_commands": [], "turns": [],
                "transcript_sha256": "a" * 64}
        for scored_as, stage, message in stages:
            item = {"scored_as": scored_as, "stage": stage, "message": message, "response": fixture(scored_as),
                    "planner_calls": 1, "host_correction_calls": 0,
                    "confirmation_revoked_before_every_call": True, "context_unchanged": True,
                    "state_after": "clarifying" if scored_as in quality.NEGATIVE_SCENARIOS else "proposed"}
            if scored_as in quality.NEGATIVE_SCENARIOS:
                item["blocked_build_calls"] = 0
            case["turns"].append(item)
        if name == "restart_current_decision":
            case["restart_evidence"] = {"prepare_process_id": 101, "resume_process_id": 102,
                                       "second_process": True, "returncode": 0}
        case["evidence_sha256"] = quality.evidence_digest(case)
        cases.append(case)
    report = {"matrix_version": quality.MATRIX_VERSION, "automatic_passed": True,
              "source_sha256": {"source.py": "b" * 64}, "scenarios": cases}
    review = {"kind": "forge_dialogue_quality_semantic_review", "matrix_version": quality.MATRIX_VERSION,
              "reviewer": "Independent fixture reviewer", "report_sha256": "c" * 64,
              "source_sha256": copy.deepcopy(report["source_sha256"]), "scenarios": []}
    for case in cases:
        review["scenarios"].append({"name": case["name"], "evidence_sha256": case["evidence_sha256"],
            "answers": {key: True for key in quality.RUBRICS[case["name"]]},
            "reason": "The exact fixture addresses the fixed current instruction and all required boundaries."})
    return report, review


class DialogueQualityOracleTests(unittest.TestCase):
    def test_exact_current_greetings_classifier_and_boolean_validator_pass_mechanical_oracle(self):
        for scenario in ("saved_baseline", "greeting_only_change", "restart_current_decision",
                         "explicit_equality", "contextual_validator"):
            with self.subTest(scenario=scenario):
                self.assertTrue(quality.score_response(scenario, proposed(scenario))["required_cases"])

    def test_no_required_edge_case_can_be_omitted(self):
        for scenario in ("saved_baseline", "greeting_only_change", "explicit_equality", "contextual_validator"):
            valid = proposed(scenario)
            for index in range(len(valid["task"]["checks"][0]["cases"])):
                response = copy.deepcopy(valid)
                del response["task"]["checks"][0]["cases"][index]
                with self.subTest(scenario=scenario, index=index), self.assertRaises(ValueError):
                    quality.score_response(scenario, response)

    def test_wrong_scalar_type_and_extra_wrong_case_fail(self):
        response = proposed("contextual_validator")
        response["task"]["checks"][0]["cases"][0]["expected"] = 0
        with self.assertRaises(ValueError):
            quality.score_response("contextual_validator", response)
        response = proposed()
        response["task"]["checks"][0]["cases"].append({"args": ["Zoe"], "expected": "invented"})
        with self.assertRaises(ValueError):
            quality.score_response("saved_baseline", response)

    def test_old_greeting_empty_and_trimming_regressions_fail(self):
        for index, wrong in ((0, "Guten Tag, Ada!"), (2, "Servus, !"), (3, quality.EMPTY_TEXT), (4, "Servus, Ada!")):
            response = proposed("greeting_only_change")
            response["task"]["checks"][0]["cases"][index]["expected"] = wrong
            with self.subTest(index=index), self.assertRaises(ValueError):
                quality.score_response("greeting_only_change", response)

    def test_classifier_wrong_arity_wrong_case_and_wrong_literal_fail(self):
        for mutation in ("arity", "trim", "bool"):
            response = proposed("explicit_equality")
            case = response["task"]["checks"][0]["cases"][4]
            if mutation == "arity":
                case["args"] = [" x "]
            elif mutation == "trim":
                case["expected"] = "TREFFER#7"
            else:
                case["expected"] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                quality.score_response("explicit_equality", response)

    def test_objective_must_contain_actual_current_literals(self):
        response = proposed("greeting_only_change")
        response["task"]["objective"] = quality.greeting_objective(quality.BASE_PREFIX)
        with self.assertRaises(ValueError):
            quality.score_response("greeting_only_change", response)

    def test_redundant_questions_fail_clear_cases_and_multiple_questions_fail_negative(self):
        response = proposed()
        response["questions"] = ["Was soll bei leerem Namen geschehen?"]
        response["task"] = None
        with self.assertRaises(ValueError):
            quality.score_response("saved_baseline", response)
        for question in ("Welches Ergebnis? Und welcher Dateiname?", "Welches Ergebnis."):
            response = clarification("ambiguous_output")
            response["questions"] = [question]
            with self.assertRaises(ValueError):
                quality.score_response("ambiguous_output", response)

    def test_semantically_wrong_question_never_becomes_automatic_semantic_success(self):
        response = clarification("greeting_conflict")
        response["questions"] = ["Welche Dateinamen sollen Hi und Servus haben?"]
        result = quality.score_response("greeting_conflict", response)
        self.assertIn("one_question_resolves_actual_hi_servus_choice_for_same_input", result["semantic_rubric"])
        self.assertNotIn("passed", result)


class DialogueQualityReviewTests(unittest.TestCase):
    def test_complete_exact_review_passes(self):
        report, review = reviewed_fixture()
        self.assertTrue(quality.validate_review(report, review, "c" * 64))

    def test_review_cannot_override_failure_or_missing_rubric_or_false_claim(self):
        for change in ("automatic", "missing", "false", "override", "type"):
            report, review = reviewed_fixture()
            if change == "automatic":
                report["automatic_passed"] = False
            elif change == "missing":
                review["scenarios"][0]["answers"].pop(next(iter(review["scenarios"][0]["answers"])))
            elif change == "false":
                review["scenarios"][0]["answers"][next(iter(review["scenarios"][0]["answers"]))] = False
            elif change == "override":
                review["passed"] = True
            else:
                review["scenarios"][0]["answers"][next(iter(review["scenarios"][0]["answers"]))] = 1
            with self.subTest(change=change), self.assertRaises(ValueError):
                quality.validate_review(report, review, "c" * 64)

    def test_changed_response_report_source_or_raw_digest_cannot_use_old_review(self):
        for change in ("response", "report", "source", "transcript"):
            report, review = reviewed_fixture()
            if change == "response":
                report["scenarios"][0]["turns"][0]["response"]["reply"] = "changed"
            elif change == "report":
                review["report_sha256"] = "d" * 64
            elif change == "source":
                review["source_sha256"]["source.py"] = "d" * 64
            else:
                report["scenarios"][0]["transcript_sha256"] = "d" * 64
            with self.subTest(change=change), self.assertRaises(ValueError):
                quality.validate_review(report, review, "c" * 64)

    def test_same_pid_retry_missing_turn_and_wrong_cases_fail_even_with_updated_review_digest(self):
        for change in ("pid", "retry", "missing", "cases"):
            report, review = reviewed_fixture()
            index = 6 if change == "pid" else 0
            case = report["scenarios"][index]
            if change == "pid":
                case["restart_evidence"]["resume_process_id"] = 101
            elif change == "retry":
                case["turns"][0]["planner_calls"] = 2
            elif change == "missing":
                case["turns"] = []
            else:
                case["turns"][0]["response"]["task"]["checks"][0]["cases"][0]["expected"] = "wrong"
            case["evidence_sha256"] = quality.evidence_digest(case)
            review["scenarios"][index]["evidence_sha256"] = case["evidence_sha256"]
            with self.subTest(change=change), self.assertRaises(ValueError):
                quality.validate_review(report, review, "c" * 64)

    def test_apply_review_rereads_actual_raw_transcript(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            report, review = reviewed_fixture()
            for case in report["scenarios"]:
                child = directory / case["name"]
                child.mkdir()
                raw = b'{"actual":"raw evidence"}\n'
                (child / "model_dialogue.jsonl").write_bytes(raw)
                case["transcript_sha256"] = hashlib.sha256(raw).hexdigest()
                case["evidence_sha256"] = quality.evidence_digest(case)
                quality.write_new(child, "scenario_report.json", case)
            for case, item in zip(report["scenarios"], review["scenarios"]):
                item["evidence_sha256"] = case["evidence_sha256"]
            quality.write_new(directory, "dialogue_quality_run_report.json", report)
            review["report_sha256"] = hashlib.sha256((directory / "dialogue_quality_run_report.json").read_bytes()).hexdigest()
            quality.write_new(directory, "review.json", review)
            (directory / quality.SCENARIOS[0] / "model_dialogue.jsonl").write_text("changed\n", encoding="utf-8")
            with patch.object(quality, "ROOT", directory), patch.object(quality, "source_hashes", return_value=report["source_sha256"]):
                with self.assertRaisesRegex(ValueError, "Rohprotokoll"):
                    quality.apply_review(directory, directory / "review.json")
            self.assertFalse((directory / "dialogue_quality_acceptance_report.json").exists())


class DialogueQualityRuntimeEvidenceTests(unittest.TestCase):
    def test_current_change_revokes_old_confirmation_before_actual_call(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            report = {"host_commands": [], "turns": []}
            with TaskStore(directory).exclusive() as store:
                task = ConversationTask(store)
                quality.setup(task, "greeting_only_change", report)
                quality.turn(task, "saved_baseline", quality.CURRENT_MESSAGES["saved_baseline"],
                             lambda _: json.dumps(proposed()), directory, report, "baseline")
                task.confirm()
                self.assertTrue(task.is_confirmed())
                def current(messages):
                    self.assertFalse(task.is_confirmed())
                    self.assertIsNone(task.data["confirmed_proposal_sha256"])
                    self.assertIsNone(task.data["confirmed_context_sha256"])
                    self.assertIsNone(task.data["proposal"])
                    return json.dumps(proposed("greeting_only_change"))
                quality.turn(task, "greeting_only_change", quality.CURRENT_MESSAGES["greeting_only_change"],
                             current, directory, report, "current_message")
            self.assertEqual(report["turns"][-1]["state_before"], "ready")
            self.assertTrue(report["turns"][-1]["confirmation_revoked_before_every_call"])
            self.assertEqual(len((directory / "model_dialogue.jsonl").read_text(encoding="utf-8").splitlines()), 2)

    def test_invalid_response_is_retained_and_fails_without_second_call(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            report = {"host_commands": [], "turns": []}
            replies = iter(("invalid JSON", json.dumps(proposed())))
            with TaskStore(directory).exclusive() as store:
                task = ConversationTask(store)
                quality.setup(task, "saved_baseline", report)
                with self.assertRaisesRegex(ValueError, "Planneraufruf"):
                    quality.turn(task, "saved_baseline", quality.CURRENT_MESSAGES["saved_baseline"],
                                 lambda _: next(replies), directory, report, "current_message")
            self.assertEqual(report["turns"][0]["planner_calls"], 1)
            self.assertEqual(report["turns"][0]["host_correction_calls"], 0)
            raw = (directory / "model_dialogue.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(raw), 1)
            self.assertEqual(json.loads(raw[0])["response"], "invalid JSON")

    def test_negative_case_blocks_real_build_entry_point_and_keeps_raw_response(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            report = {"host_commands": [], "turns": []}
            with TaskStore(directory).exclusive() as store:
                task = ConversationTask(store)
                quality.setup(task, "unsupported_actions", report)
                quality.turn(task, "unsupported_actions", quality.CURRENT_MESSAGES["unsupported_actions"],
                             lambda _: json.dumps(clarification("unsupported_actions")), directory, report, "current_message")
            self.assertEqual(report["turns"][0]["blocked_build_calls"], 0)
            self.assertFalse((directory / "build").exists())
            raw = json.loads((directory / "model_dialogue.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(json.loads(raw["response"]), clarification("unsupported_actions"))

    def test_reports_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            quality.write_new(directory, "evidence.json", {"passed": False})
            with self.assertRaises(FileExistsError):
                quality.write_new(directory, "evidence.json", {"passed": True})
            self.assertEqual(quality.read_json(directory / "evidence.json"), {"passed": False})


if __name__ == "__main__":
    unittest.main()
