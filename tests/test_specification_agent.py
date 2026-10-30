import unittest

from src.specification_agent import build_specification

class TestSpecificationAgent(unittest.TestCase):
    def test_complete_valid_brief(self):
        """Test that a complete valid brief produces correct specification."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1", "input2"],
            "outputs": ["output1", "output2"],
            "constraints": ["constraint1", "constraint2"]
        }
        
        expected = """## Name
Test Agent

## Purpose
To test the specification agent

## Inputs
- input1
- input2

## Outputs
- output1
- output2

## Constraints
- constraint1
- constraint2
"""
        
        result = build_specification(brief)
        self.assertEqual(result, expected)
        self.assertTrue(result.endswith("\n"))
        self.assertFalse(result.endswith("\n\n"))
    
    def test_missing_name(self):
        """Test that missing name raises ValueError."""
        brief = {
            "purpose": "To test the specification agent",
            "inputs": ["input1"],
            "outputs": ["output1"],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Missing required field: name", str(context.exception))
    
    def test_missing_purpose(self):
        """Test that missing purpose raises ValueError."""
        brief = {
            "name": "Test Agent",
            "inputs": ["input1"],
            "outputs": ["output1"],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Missing required field: purpose", str(context.exception))
    
    def test_missing_inputs(self):
        """Test that missing inputs raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "outputs": ["output1"],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Missing required field: inputs", str(context.exception))
    
    def test_missing_outputs(self):
        """Test that missing outputs raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1"],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Missing required field: outputs", str(context.exception))
    
    def test_missing_constraints(self):
        """Test that missing constraints raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1"],
            "outputs": ["output1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Missing required field: constraints", str(context.exception))
    
    def test_empty_name(self):
        """Test that empty name raises ValueError."""
        brief = {
            "name": "",
            "purpose": "To test the specification agent",
            "inputs": ["input1"],
            "outputs": ["output1"],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Field 'name' must be a non-empty string", str(context.exception))
    
    def test_empty_purpose(self):
        """Test that empty purpose raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "",
            "inputs": ["input1"],
            "outputs": ["output1"],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Field 'purpose' must be a non-empty string", str(context.exception))
    
    def test_non_list_inputs(self):
        """Test that non-list inputs raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": "not_a_list",
            "outputs": ["output1"],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Field 'inputs' must be a list", str(context.exception))
    
    def test_non_list_outputs(self):
        """Test that non-list outputs raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1"],
            "outputs": "not_a_list",
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Field 'outputs' must be a list", str(context.exception))
    
    def test_non_list_constraints(self):
        """Test that non-list constraints raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1"],
            "outputs": ["output1"],
            "constraints": "not_a_list"
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Field 'constraints' must be a list", str(context.exception))
    
    def test_empty_input_item(self):
        """Test that empty input item raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["", "input2"],
            "outputs": ["output1"],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Input at index 0 must be a non-empty string", str(context.exception))
    
    def test_empty_output_item(self):
        """Test that empty output item raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1"],
            "outputs": ["", "output2"],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Output at index 0 must be a non-empty string", str(context.exception))
    
    def test_empty_constraint_item(self):
        """Test that empty constraint item raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1"],
            "outputs": ["output1"],
            "constraints": ["", "constraint2"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Constraint at index 0 must be a non-empty string", str(context.exception))
    
    def test_non_string_inputs_item(self):
        """Test that non-string inputs item raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1", 123],
            "outputs": ["output1"],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Input at index 1 must be a non-empty string", str(context.exception))
    
    def test_non_string_outputs_item(self):
        """Test that non-string outputs item raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1"],
            "outputs": ["output1", 456],
            "constraints": ["constraint1"]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Output at index 1 must be a non-empty string", str(context.exception))
    
    def test_non_string_constraints_item(self):
        """Test that non-string constraints item raises ValueError."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1"],
            "outputs": ["output1"],
            "constraints": ["constraint1", 789]
        }
        
        with self.assertRaises(ValueError) as context:
            build_specification(brief)
            
        self.assertIn("Constraint at index 1 must be a non-empty string", str(context.exception))
    
    def test_determinism(self):
        """Test that the same input always produces the same output."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input1", "input2"],
            "outputs": ["output1", "output2"],
            "constraints": ["constraint1", "constraint2"]
        }
        
        result1 = build_specification(brief)
        result2 = build_specification(brief)
        
        self.assertEqual(result1, result2)
    
    def test_order_preservation(self):
        """Test that input order is preserved."""
        brief = {
            "name": "Test Agent",
            "purpose": "To test the specification agent",
            "inputs": ["input3", "input1", "input2"],
            "outputs": ["output2", "output1"],
            "constraints": ["constraint3", "constraint1", "constraint2"]
        }
        
        result = build_specification(brief)
        
        # Check that the order is preserved
        self.assertIn("- input3\n- input1\n- input2", result)
        self.assertIn("- output2\n- output1", result)
        self.assertIn("- constraint3\n- constraint1\n- constraint2", result)

if __name__ == '__main__':
    unittest.main()