import unittest
from src.specification_agent.core import build_specification

class TestSpecificationAgentWhitespace(unittest.TestCase):
    
    def test_name_only_whitespace_raises_valueerror(self):
        """Test that a name only consisting of whitespace raises ValueError with 'name' in message."""
        with self.assertRaises(ValueError) as context:
            build_specification(brief={"name": "   ", "purpose": "Valid purpose", "inputs": [], "outputs": [], "constraints": []})
        
        self.assertIn("name", str(context.exception))
    
    def test_purpose_only_whitespace_raises_valueerror(self):
        """Test that a purpose only consisting of whitespace raises ValueError with 'purpose' in message."""
        with self.assertRaises(ValueError) as context:
            build_specification(brief={"name": "Valid name", "purpose": "   ", "inputs": [], "outputs": [], "constraints": []})
        
        self.assertIn("purpose", str(context.exception))
    
    def test_constraint_only_whitespace_raises_valueerror(self):
        """Test that a constraint only consisting of whitespace raises ValueError with 'Constraint at index 0' in message."""
        with self.assertRaises(ValueError) as context:
            build_specification(brief={"name": "Valid name", "purpose": "Valid purpose", "inputs": [], "outputs": [], "constraints": ["   "]})
        
        self.assertIn("Constraint at index 0", str(context.exception))

if __name__ == '__main__':
    unittest.main()