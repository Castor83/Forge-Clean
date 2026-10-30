def build_specification(brief: dict[str, object]) -> str:
    """
    Build a deterministic Markdown specification from a brief dictionary.
    
    Args:
        brief: Dictionary containing 'name', 'purpose', 'inputs', 'outputs', and 'constraints'
        
    Returns:
        str: Markdown formatted specification
        
    Raises:
        ValueError: If any required field is missing or invalid
    """
    # Validate required fields
    required_fields = ["name", "purpose", "inputs", "outputs", "constraints"]
    for field in required_fields:
        if field not in brief:
            raise ValueError(f"Missing required field: {field}")
    
    # Validate field types
    if not isinstance(brief["name"], str) or not brief["name"].strip():
        raise ValueError("Field 'name' must be a non-empty string")
        
    if not isinstance(brief["purpose"], str) or not brief["purpose"].strip():
        raise ValueError("Field 'purpose' must be a non-empty string")
        
    if not isinstance(brief["inputs"], list):
        raise ValueError("Field 'inputs' must be a list")
        
    if not isinstance(brief["outputs"], list):
        raise ValueError("Field 'outputs' must be a list")
        
    if not isinstance(brief["constraints"], list):
        raise ValueError("Field 'constraints' must be a list")
    
    # Validate list contents
    for i, item in enumerate(brief["inputs"]):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Input at index {i} must be a non-empty string")
            
    for i, item in enumerate(brief["outputs"]):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Output at index {i} must be a non-empty string")
            
    for i, item in enumerate(brief["constraints"]):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Constraint at index {i} must be a non-empty string")
    
    # Build specification
    spec = []
    spec.append("## Name")
    spec.append(brief["name"])
    spec.append("")
    spec.append("## Purpose")
    spec.append(brief["purpose"])
    spec.append("")
    spec.append("## Inputs")
    for item in brief["inputs"]:
        spec.append(f"- {item}")
    spec.append("")
    spec.append("## Outputs")
    for item in brief["outputs"]:
        spec.append(f"- {item}")
    spec.append("")
    spec.append("## Constraints")
    for item in brief["constraints"]:
        spec.append(f"- {item}")
    spec.append("")
    
    result = "\n".join(spec)
    # Ensure exactly one trailing newline
    if not result.endswith("\n"):
        result += "\n"
    return result