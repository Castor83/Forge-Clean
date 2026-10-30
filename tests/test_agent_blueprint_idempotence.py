import unittest
from src.agent_blueprint.naming import normalize_agent_name

class TestAgentBlueprintIdempotence(unittest.TestCase):
    
    def test_already_normalized_string(self):
        """Test that normalize_agent_name("report-builder") results in "report-builder"."""
        result = normalize_agent_name("report-builder")
        self.assertEqual(result, "report-builder")
    
    def test_double_normalization(self):
        """Test that double normalization of "  Forge__Agent 2  " gives "forge-agent-2" after both calls."""
        # First normalization
        result1 = normalize_agent_name("  Forge__Agent 2  ")
        self.assertEqual(result1, "forge-agent-2")
        
        # Second normalization
        result2 = normalize_agent_name(result1)
        self.assertEqual(result2, "forge-agent-2")
    
    def test_idempotence_of_hyphen_processing(self):
        """Test that input "A---B" remains "a-b" after re-normalization."""
        # First normalization
        result1 = normalize_agent_name("A---B")
        self.assertEqual(result1, "a-b")
        
        # Second normalization
        result2 = normalize_agent_name(result1)
        self.assertEqual(result2, "a-b")

if __name__ == '__main__':
    unittest.main()