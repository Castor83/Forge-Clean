import unittest
from src.agent_blueprint.validation import is_valid_agent_identifier

class TestAgentBlueprintValidation(unittest.TestCase):
    
    def test_valid_minimal_identifier(self):
        """Test valid minimal identifier."""
        self.assertTrue(is_valid_agent_identifier("a"))
    
    def test_valid_max_length_identifier(self):
        """Test valid maximum length identifier."""
        identifier = "a" * 40
        self.assertTrue(is_valid_agent_identifier(identifier))
    
    def test_valid_identifier_with_digits(self):
        """Test valid identifier with digits."""
        self.assertTrue(is_valid_agent_identifier("agent123"))
    
    def test_valid_identifier_with_hyphens(self):
        """Test valid identifier with hyphens."""
        self.assertTrue(is_valid_agent_identifier("agent-name-123"))
    
    def test_valid_identifier_starting_with_letter_ending_with_letter(self):
        """Test valid identifier starting with letter and ending with letter."""
        self.assertTrue(is_valid_agent_identifier("test-agent"))
    
    def test_valid_identifier_starting_with_letter_ending_with_digit(self):
        """Test valid identifier starting with letter and ending with digit."""
        self.assertTrue(is_valid_agent_identifier("agent1"))
    
    def test_invalid_empty_string(self):
        """Test invalid empty string."""
        self.assertFalse(is_valid_agent_identifier(""))
    
    def test_invalid_too_long(self):
        """Test invalid identifier that is too long."""
        identifier = "a" * 41
        self.assertFalse(is_valid_agent_identifier(identifier))
    
    def test_invalid_starting_with_digit(self):
        """Test invalid identifier starting with digit."""
        self.assertFalse(is_valid_agent_identifier("1agent"))
    
    def test_invalid_ending_with_hyphen(self):
        """Test invalid identifier ending with hyphen."""
        self.assertFalse(is_valid_agent_identifier("agent-"))
    
    def test_invalid_starting_with_hyphen(self):
        """Test invalid identifier starting with hyphen."""
        self.assertFalse(is_valid_agent_identifier("-agent"))
    
    def test_invalid_consecutive_hyphens(self):
        """Test invalid identifier with consecutive hyphens."""
        self.assertFalse(is_valid_agent_identifier("agent--name"))
    
    def test_invalid_contains_underscore(self):
        """Test invalid identifier containing underscore."""
        self.assertFalse(is_valid_agent_identifier("agent_name"))
    
    def test_invalid_contains_space(self):
        """Test invalid identifier containing space."""
        self.assertFalse(is_valid_agent_identifier("agent name"))
    
    def test_invalid_contains_uppercase(self):
        """Test invalid identifier containing uppercase letters."""
        self.assertFalse(is_valid_agent_identifier("Agent"))
    
    def test_invalid_non_string_types(self):
        """Test invalid non-string types."""
        self.assertFalse(is_valid_agent_identifier(None))
        self.assertFalse(is_valid_agent_identifier(123))
        self.assertFalse(is_valid_agent_identifier(True))
        self.assertFalse(is_valid_agent_identifier([]))
        self.assertFalse(is_valid_agent_identifier({}))

    def test_invalid_non_ascii_characters(self):
        """Test invalid identifiers with non-ASCII characters."""
        # Test umlauts
        self.assertFalse(is_valid_agent_identifier("ägent"))
        self.assertFalse(is_valid_agent_identifier("agenté"))
        
        # Test Cyrillic
        self.assertFalse(is_valid_agent_identifier("агент"))
        
        # Test Greek
        self.assertFalse(is_valid_agent_identifier("agent-β"))
        
    def test_invalid_unicode_digits(self):
        """Test invalid identifiers with Unicode digits."""
        # Test Unicode Arabic-Indic digits
        self.assertFalse(is_valid_agent_identifier("agent١"))
        self.assertFalse(is_valid_agent_identifier("agent-١"))
        
        # Test Fullwidth ASCII digits
        self.assertFalse(is_valid_agent_identifier("agent１"))
        self.assertFalse(is_valid_agent_identifier("a１"))
        
    def test_signature_annotations(self):
        """Test that function signature annotations are correct."""
        import inspect
        sig = inspect.signature(is_valid_agent_identifier)
        params = sig.parameters
        self.assertIn('identifier', params)
        self.assertEqual(params['identifier'].annotation, object)
        self.assertEqual(sig.return_annotation, bool)

if __name__ == '__main__':
    unittest.main()