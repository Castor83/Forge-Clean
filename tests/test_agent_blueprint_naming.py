import unittest
from src.agent_blueprint.naming import normalize_agent_name

class TestAgentBlueprintNaming(unittest.TestCase):
    
    def test_non_string_input_raises_value_error(self):
        """Rule 1: Other input types than str raise ValueError; message contains 'name'."""
        with self.assertRaises(ValueError) as context:
            normalize_agent_name(123)
        
        self.assertIn("name", str(context.exception))
        
        with self.assertRaises(ValueError) as context:
            normalize_agent_name(None)
        
        self.assertIn("name", str(context.exception))
    
    def test_leading_trailing_whitespace_removed(self):
        """Rule 2: Leading and trailing whitespace removed."""
        result = normalize_agent_name("  Report Builder  ")
        self.assertEqual(result, "report-builder")
    
    def test_ascii_letters_lowercase(self):
        """Rule 3: ASCII letters converted to lowercase."""
        result = normalize_agent_name("REPORT BUILDER")
        self.assertEqual(result, "report-builder")
    
    def test_non_alphanumeric_replaced_with_hyphen(self):
        """Rule 4: Sequences of non-alphanumeric characters replaced with single hyphen."""
        result = normalize_agent_name("A...B___C")
        self.assertEqual(result, "a-b-c")
    
    def test_leading_trailing_hyphens_removed(self):
        """Rule 5: Leading and trailing hyphens removed."""
        result = normalize_agent_name("---report-builder---")
        self.assertEqual(result, "report-builder")
    
    def test_empty_result_raises_value_error(self):
        """Rule 6: Empty result raises ValueError; message contains 'name'."""
        with self.assertRaises(ValueError) as context:
            normalize_agent_name("---")
        
        self.assertIn("name", str(context.exception))
    
    def test_examples_work_correctly(self):
        """Test all example cases work correctly."""
        # Test case 1
        result = normalize_agent_name("  Report Builder  ")
        self.assertEqual(result, "report-builder")
        
        # Test case 2
        result = normalize_agent_name("Forge__Agent 2")
        self.assertEqual(result, "forge-agent-2")
        
        # Test case 3
        result = normalize_agent_name("A...B___C")
        self.assertEqual(result, "a-b-c")
        
        # Test case 4
        with self.assertRaises(ValueError) as context:
            normalize_agent_name("---")
        
        self.assertIn("name", str(context.exception))

if __name__ == '__main__':
    unittest.main()