"""Question punctuation is a host contract, never a semantic meaning gate."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_protocol import discuss, parse_conversation_response
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.lm_studio_executor import LMStudioChatClient


def question_response(questions):
    return json.dumps({"reply": "Zur gewünschten Regel fehlt noch eine Angabe.",
                       "questions": questions, "task": None}, ensure_ascii=False)


def punctuation_proposal():
    return {"reply": "Die Ausgabe endet genau mit einem Fragezeichen: <Name>?",
            "questions": [], "task": {
                "task_id": "conversation-build",
                "objective": "Eine importfreie return-Funktion gibt exakt den unveränderten Namen plus '?' zurück; Ada ergibt 'Ada?', leer ergibt '?'.",
                "allowed_files": ["agent/question_text.py"], "checks": [{
                    "kind": "python_function_cases", "path": "agent/question_text.py",
                    "function": "question_text", "cases": [
                        {"args": ["Ada"], "expected": "Ada?"},
                        {"args": [""], "expected": "?"}]}]}}


class ConversationQuestionShapeTests(unittest.TestCase):
    def test_single_and_three_questions_are_preserved_exactly(self):
        for questions in (["Was macht den Text gültig?"],
                          ["Welche Ausgabe gilt?", "Was bedeutet leer?", "Welche Eingaben gelten?"],
                          ["Q" * 999 + "?"]):
            with self.subTest(questions=questions):
                parsed = parse_conversation_response(question_response(questions))
                self.assertEqual(parsed["questions"], questions)
                self.assertIsNone(parsed["task"])

    def test_question_shape_does_not_judge_the_question_meaning(self):
        questions = ["Ist ein runder Kreis rund?"]
        self.assertEqual(parse_conversation_response(question_response(questions))["questions"], questions)

    def test_invalid_question_shapes_are_rejected(self):
        invalid = (
            "?", "   ?", "\t\n?", "Welche Regel gilt", "Welche Regel gilt.",
            "Welche Regel? Zum Beispiel sichtbare Zeichen.",
            "Welche Regel? Welche Ausgabe?", "Welche Regel??",
            "Welche Regel?\n", "Welche Regel? ", "Q" * 1000 + "?",
        )
        for question in invalid:
            with self.subTest(question=question), self.assertRaises(ValueError):
                parse_conversation_response(question_response([question]))

    def test_four_questions_remain_rejected(self):
        with self.assertRaises(ValueError):
            parse_conversation_response(question_response(["Eine?", "Zwei?", "Drei?", "Vier?"]))

    def test_escaped_question_marks_are_validated_after_json_decoding(self):
        for encoded in (r"Welche Regel\u003f Danach ein Beispiel.",
                        r"Welche Regel\u003f Welche Ausgabe\u003f", r"\u003f"):
            raw = '{"reply":"Klärung","questions":["' + encoded + '"],"task":null}'
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                parse_conversation_response(raw)
        valid = r'{"reply":"Klärung","questions":["Welche Regel gilt\u003f"],"task":null}'
        self.assertEqual(parse_conversation_response(valid)["questions"], ["Welche Regel gilt?"])

    def test_question_mark_in_objective_reply_and_exact_outputs_is_unchanged(self):
        original = punctuation_proposal()
        result = parse_conversation_response(json.dumps(original, ensure_ascii=False))
        self.assertEqual(result, original)
        self.assertEqual(result["task"]["checks"][0]["cases"][1]["expected"], "?")
        self.assertEqual(result["questions"], [])

    def test_four_task_question_combinations_keep_existing_parse_invariant(self):
        proposal = punctuation_proposal()["task"]
        for task, questions, accepted in ((None, ["Welche Regel gilt?"], True),
                                          (proposal, [], True),
                                          (None, [], False),
                                          (proposal, ["Welche Regel gilt?"], False)):
            response = {"reply": "Aktuelle Antwort.", "questions": questions, "task": task}
            with self.subTest(task_present=task is not None, questions=questions):
                if accepted:
                    self.assertEqual(parse_conversation_response(json.dumps(response)), response)
                else:
                    with self.assertRaises(ValueError):
                        parse_conversation_response(json.dumps(response))


class ConversationQuestionShapeCorrectionTests(unittest.TestCase):
    def confirmed_task(self, store):
        task = ConversationTask(store)
        task.initialize_project("question_shape")
        discuss(task, "Gib den Namen mit abschließendem Fragezeichen zurück.",
                lambda _: json.dumps(punctuation_proposal(), ensure_ascii=False))
        task.confirm()
        self.assertTrue(task.is_confirmed())
        self.assertIsNotNone(task.data["confirmed_proposal_sha256"])
        self.assertIsNotNone(task.data["confirmed_context_sha256"])
        return task

    def test_invalid_first_shape_gets_one_correction_with_original_request_and_no_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            calls = []
            current_message = "Ich möchte eine andere Ausgabe, weiß aber noch nicht welche."
            replies = iter((question_response(["Welche Ausgabe? Zum Beispiel ein anderer Text."]),
                            question_response(["Wie soll die neue Ausgabe genau lauten?"])))
            with TaskStore(directory).exclusive() as store:
                task = self.confirmed_task(store)
                def chat(messages):
                    calls.append(copy.deepcopy(messages))
                    self.assertEqual(task.data["state"], "clarifying")
                    self.assertIsNone(task.data["proposal"])
                    self.assertIsNone(task.data["confirmed_proposal_sha256"])
                    self.assertIsNone(task.data["confirmed_context_sha256"])
                    self.assertFalse(task.is_confirmed())
                    return next(replies)
                result = discuss(task, current_message, chat)
                self.assertEqual(result["questions"], ["Wie soll die neue Ausgabe genau lauten?"])
                self.assertEqual(len(calls), 2)
                for messages in calls:
                    self.assertEqual(sum(role == "user" and text == current_message for role, text in messages), 1)
                feedback = json.loads(calls[1][-1][1])
                self.assertEqual(feedback["kind"], "proposal_validation_feedback")
                self.assertTrue(feedback["error"].strip())
                self.assertEqual(task.data["state"], "clarifying")
                self.assertFalse(task.is_confirmed())
                with self.assertRaises(ValueError):
                    task.confirm()
                attempted_builds = []
                with self.assertRaises(ValueError):
                    build_confirmed(task, lambda messages: attempted_builds.append(messages), lambda _: None)
                self.assertEqual(attempted_builds, [])
                self.assertFalse((directory / "build").exists())
            with TaskStore(directory).exclusive() as reopened_store:
                reopened = ConversationTask(reopened_store)
                self.assertFalse(reopened.is_confirmed())
                self.assertEqual(reopened.data["questions"], result["questions"])

    def test_two_invalid_shapes_stop_with_original_message_and_confirmation_revoked_on_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            calls = []
            message = "Ändere die Ausgabe."
            replies = iter((question_response(["Welche Ausgabe? Hier ist ein Beispiel."]),
                            question_response(["Welche Ausgabe? Oder welcher Dateiname?"])))
            with TaskStore(directory).exclusive() as store:
                task = self.confirmed_task(store)
                def chat(messages):
                    calls.append(copy.deepcopy(messages))
                    self.assertFalse(task.is_confirmed())
                    return next(replies)
                with self.assertRaises(ValueError):
                    discuss(task, message, chat)
                self.assertEqual(len(calls), 2)
                self.assertEqual(json.loads(calls[1][-1][1])["kind"], "proposal_validation_feedback")
                self.assertEqual(task.data["messages"][-1], {"role": "user", "content": message})
                self.assertEqual(task.data["state"], "clarifying")
                self.assertIsNone(task.data["proposal"])
                self.assertIsNone(task.data["confirmed_proposal_sha256"])
                self.assertIsNone(task.data["confirmed_context_sha256"])
                self.assertFalse((directory / "build").exists())
            with TaskStore(directory).exclusive() as reopened_store:
                reopened = ConversationTask(reopened_store)
                self.assertFalse(reopened.is_confirmed())
                self.assertEqual(reopened.data["messages"][-1], {"role": "user", "content": message})


class ConversationQuestionShapeTransportTests(unittest.TestCase):
    def test_actual_http_request_limits_model_to_one_question_while_host_accepts_three(self):
        observed = []
        content = question_response(["Welche Regel gilt?"])
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        def opener(request, timeout):
            observed.append((request, timeout))
            return Response()
        client = LMStudioChatClient("forge-30b", opener=opener, timeout_seconds=300, response_kind="conversation")
        self.assertEqual(client((("system", "rules"), ("user", "current request"))), content)
        self.assertEqual(len(observed), 1)
        request, timeout = observed[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(timeout, 300)
        payload = json.loads(request.data)
        schema = payload["response_format"]["json_schema"]["schema"]
        self.assertEqual(set(schema), {"anyOf"})
        self.assertEqual(len(schema["anyOf"]), 2)
        clarification, proposal = schema["anyOf"]
        for branch in (clarification, proposal):
            self.assertEqual(branch["type"], "object")
            self.assertIs(branch["additionalProperties"], False)
            self.assertEqual(set(branch["properties"]), {"reply", "questions", "task"})
            self.assertEqual(set(branch["required"]), {"reply", "questions", "task"})
        self.assertEqual(clarification["properties"]["task"], {"type": "null"})
        self.assertEqual(proposal["properties"]["task"]["type"], "object")
        self.assertIs(proposal["properties"]["task"]["additionalProperties"], False)
        self.assertNotIn("anyOf", proposal["properties"]["task"])
        self.assertEqual(proposal["properties"]["questions"]["maxItems"], 0)
        questions = clarification["properties"]["questions"]
        self.assertEqual(questions["minItems"], 1)
        self.assertEqual(questions["maxItems"], 1)
        self.assertEqual(questions["items"]["pattern"], r"^[^?]+\?$")
        self.assertEqual(questions["items"]["maxLength"], 1000)
        self.assertEqual(questions["items"]["minLength"], 2)
        self.assertIs(payload["response_format"]["json_schema"]["strict"], True)
        self.assertNotIn("tools", payload)


if __name__ == "__main__":
    unittest.main()
