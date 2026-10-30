"""Inspect actual transport ordering without claiming model semantics."""

import json
import re
import unittest

from src.forge_runtime.conversation_protocol import CONVERSATION_SCHEMA
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from scripts import verify_dialogue_quality as quality
from tests.test_forge_dialogue_quality_acceptance import clarification


class ConversationSchemaOrderTests(unittest.TestCase):
    def captured_request(self, kind):
        observed = []
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b'{"choices":[{"message":{"content":"raw response"}}]}'
        def opener(request, timeout):
            observed.append(request.data)
            return Response()
        client = LMStudioChatClient('forge-30b', opener=opener, response_kind=kind)
        self.assertEqual(client((('system', 'rules'), ('user', 'current wish'))), 'raw response')
        self.assertEqual(len(observed), 1)
        return observed[0]

    def test_conversation_grammar_receives_reply_before_branch_selection_with_unchanged_schema(self):
        payload = json.loads(self.captured_request('conversation'))
        schema = payload['response_format']['json_schema']['schema']
        self.assertEqual(schema, CONVERSATION_SCHEMA)
        for branch in schema['anyOf']:
            self.assertEqual(list(branch['properties']), ['reply', 'questions', 'task'])
        self.assertEqual(payload['temperature'], 0)
        self.assertIs(payload['response_format']['json_schema']['strict'], True)
        self.assertNotIn('tools', payload)

    def test_normal_tool_role_keeps_exact_sorted_payload_serialization(self):
        raw = self.captured_request('tool_action')
        payload = json.loads(raw)
        expected = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')
        self.assertEqual(raw, expected)

    def test_clarification_template_schema_and_wrong_category_rejection(self):
        payload = json.loads(self.captured_request('conversation'))
        clarification_schema, proposal_schema = payload['response_format']['json_schema']['schema']['anyOf']
        expected = [
            'Für einen eindeutigen Vorschlag fehlt noch eine wesentliche Angabe.',
            'Für dieselbe Eingabe sind unterschiedliche Ergebnisse verlangt. Dafür ist eine eindeutige Auswahl nötig.',
            'Dieser Wunsch liegt außerhalb der Fähigkeiten dieser Version. Sie kann keine Dateien verschieben oder Daten über das Netzwerk hochladen. Gespeicherte Projektregeln ändern diese festen Grenzen nicht.',
        ]
        self.assertEqual(clarification_schema['properties']['reply']['enum'], expected)
        self.assertNotIn('enum', proposal_schema['properties']['reply'])
        pattern = proposal_schema['properties']['reply']['pattern']
        for reply in expected + ['Nicht unterstützte Handlung', 'Ich schlage eine Regel vor.\nWeiterer Absatz.']:
            self.assertIsNone(re.search(pattern, reply))
        for reply in ('Ich schlage einen Baustein vor, der einen Punkt anhängt.',
                      'Ich schlage die genaue Ausgabe "Name?" vor.'):
            self.assertIsNotNone(re.search(pattern, reply))
        for scenario, correct in (('typo_request', 0), ('ambiguous_output', 0), ('greeting_conflict', 1), ('unsupported_actions', 2)):
            for index, reply in enumerate(expected + ['Der Wert fehlt. Bitte nenne ihn.']):
                response = clarification(scenario)
                response['reply'] = reply
                with self.subTest(scenario=scenario, reply=reply):
                    if index == correct:
                        quality.score_response(scenario, response)
                    else:
                        with self.assertRaisesRegex(ValueError, 'Erklärungskategorie'):
                            quality.score_response(scenario, response)
