import unittest

from src.forge_runtime.conversation_protocol import parse_proposal_task
from tests.test_forge_conversation_protocol import proposal


class ConversationPathTests(unittest.TestCase):
    def test_lowercase_windows_devices_are_rejected_before_confirmation(self):
        for name in ("con", "prn", "aux", "nul", "com1", "lpt9"):
            with self.subTest(name=name):
                value = proposal()
                value["allowed_files"] = [f"agent/{name}.py"]
                value["checks"][0]["path"] = value["allowed_files"][0]
                with self.assertRaisesRegex(ValueError, "reserviert"):
                    parse_proposal_task(value)
