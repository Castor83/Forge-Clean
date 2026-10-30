import unittest
import json
from src.agent_blueprint.manifest import build_agent_manifest
from src.agent_blueprint.naming import normalize_agent_name
from src.agent_blueprint.validation import is_valid_agent_identifier

class TestAgentBlueprintManifest(unittest.TestCase):
    
    def test_valid_normalization(self):
        """Test that name gets normalized correctly."""
        result = build_agent_manifest("  Report Builder  ", ["Capability A", "Capability B"])
        parsed = json.loads(result)
        self.assertEqual(parsed["name"], "report-builder")
    
    def test_duplicate_removal_and_order_preservation(self):
        """Test that duplicates are removed and order is preserved."""
        result = build_agent_manifest("test", ["cap1", "cap2", "cap1", "cap3", "cap2"])
        parsed = json.loads(result)
        self.assertEqual(parsed["capabilities"], ["cap1", "cap2", "cap3"])
    
    def test_input_unchanged(self):
        """Test that inputs are not modified."""
        original_name = "  Test Name  "
        original_caps = ["Capability A", "Capability B"]
        original_caps_copy = original_caps.copy()
        
        build_agent_manifest(original_name, original_caps)
        
        # Check that originals are unchanged
        self.assertEqual(original_name, "  Test Name  ")
        self.assertEqual(original_caps, original_caps_copy)
    
    def test_empty_capabilities_list(self):
        """Test that empty capabilities list raises ValueError."""
        with self.assertRaises(ValueError) as context:
            build_agent_manifest("test", [])
        
        self.assertIn("capabilities must contain at least one item", str(context.exception))
    
    def test_non_list_capabilities(self):
        """Test that non-list capabilities raise ValueError."""
        with self.assertRaises(ValueError) as context:
            build_agent_manifest("test", "not a list")
        
        self.assertIn("capabilities must be a list", str(context.exception))
    
    def test_list_subclass_capabilities(self):
        """Test that list subclass capabilities raise ValueError."""
        class CustomList(list):
            pass
        
        with self.assertRaises(ValueError) as context:
            build_agent_manifest("test", CustomList(["cap1"]))
        
        self.assertIn("capabilities must be a list", str(context.exception))
    
    def test_tuple_capabilities(self):
        """Test that tuple capabilities raise ValueError."""
        with self.assertRaises(ValueError) as context:
            build_agent_manifest("test", ("cap1", "cap2"))
        
        self.assertIn("capabilities must be a list", str(context.exception))
    
    def test_none_name(self):
        """Test that None name raises ValueError."""
        with self.assertRaises(ValueError) as context:
            build_agent_manifest(None, ["cap"])
        
        self.assertIn("name must be a string", str(context.exception))
    
    def test_true_name(self):
        """Test that True name raises ValueError."""
        with self.assertRaises(ValueError) as context:
            build_agent_manifest(True, ["cap"])
        
        self.assertIn("name must be a string", str(context.exception))
    
    def test_non_string_capability(self):
        """Test that non-string capability raises ValueError."""
        with self.assertRaises(ValueError) as context:
            build_agent_manifest("test", ["cap1", 123])
        
        self.assertIn("all capabilities must be strings", str(context.exception))
    
    def test_string_subclass_capability(self):
        """Test that string subclass capability raises ValueError."""
        class CustomStr(str):
            pass
        
        with self.assertRaises(ValueError) as context:
            build_agent_manifest("test", ["cap1", CustomStr("cap2")])
        
        self.assertIn("all capabilities must be strings", str(context.exception))
    
    def test_whitespace_capability(self):
        """Test that whitespace-only capability raises ValueError."""
        with self.assertRaises(ValueError) as context:
            build_agent_manifest("test", ["cap1", "   "])
        
        self.assertIn("capabilities cannot be empty after stripping", str(context.exception))
    
    def test_unicode_output(self):
        """Test that Unicode characters are handled correctly with ensure_ascii=True."""
        result = build_agent_manifest("test", ["café", "naïve"])
        parsed = json.loads(result)
        self.assertEqual(parsed["capabilities"], ["café", "naïve"])
        
        # Verify it's ASCII-encoded in the output
        self.assertIn("caf\\u00e9", result)
        self.assertIn("na\\u00efve", result)
    
    def test_single_newline_at_end(self):
        """Test that exactly one newline is at the end."""
        result = build_agent_manifest("test", ["cap1"])
        self.assertTrue(result.endswith("\n"))
        self.assertFalse(result.endswith("\n\n"))
        
    def test_exact_output_format(self):
        """Test exact output format with proper JSON structure."""
        result = build_agent_manifest("  Test Name  ", ["Capability A", "Capability B"])
        # Use separators to match exact formatting without spaces
        expected = json.dumps({"name":"test-name","capabilities":["Capability A","Capability B"]}, ensure_ascii=True, separators=(',', ':')) + "\n"
        self.assertEqual(result, expected)
        
    def test_exact_reproducible_output(self):
        """Test that identical inputs produce identical outputs."""
        result1 = build_agent_manifest("  Test Name  ", ["cap1", "cap2"])
        result2 = build_agent_manifest("  Test Name  ", ["cap1", "cap2"])
        self.assertEqual(result1, result2)
        
    def test_case_sensitive_duplicates(self):
        """Test that case-sensitive duplicates are handled correctly."""
        result = build_agent_manifest("test", ["Cap", "CAP", "cap"])
        parsed = json.loads(result)
        self.assertEqual(parsed["capabilities"], ["Cap", "CAP", "cap"])

if __name__ == '__main__':
    unittest.main()