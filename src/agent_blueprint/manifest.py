import json
from .naming import normalize_agent_name
from .validation import is_valid_agent_identifier

def build_agent_manifest(name: object, capabilities: object) -> str:
    """
    Build an agent manifest as JSON string.
    
    Args:
        name (object): The agent name to normalize
        capabilities (object): The list of capabilities
        
    Returns:
        str: JSON manifest with exactly one newline at the end
        
    Raises:
        ValueError: If inputs don't meet requirements
    """
    # Validate and normalize name
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    
    normalized_name = normalize_agent_name(name)
    
    if not is_valid_agent_identifier(normalized_name):
        raise ValueError("name")
    
    # Validate capabilities - must be exactly list type, not subclass
    if type(capabilities) is not list:
        raise ValueError("capabilities must be a list")
    
    if len(capabilities) == 0:
        raise ValueError("capabilities must contain at least one item")
    
    # Process capabilities
    processed_capabilities = []
    for cap in capabilities:
        # Each capability must be exactly str type, not subclass
        if type(cap) is not str:
            raise ValueError("all capabilities must be strings")
        
        stripped_cap = cap.strip()
        if not stripped_cap:
            raise ValueError("capabilities cannot be empty after stripping")
            
        processed_capabilities.append(stripped_cap)
    
    # Remove duplicates while preserving order (case-sensitive)
    unique_capabilities = []
    seen = set()
    for cap in processed_capabilities:
        if cap not in seen:
            seen.add(cap)
            unique_capabilities.append(cap)
    
    # Build and return JSON
    manifest = {
        "name": normalized_name,
        "capabilities": unique_capabilities
    }
    
    # Return exactly one newline at the end - no spaces, exact formatting
    return json.dumps(manifest, ensure_ascii=True, separators=(',', ':')) + "\n"