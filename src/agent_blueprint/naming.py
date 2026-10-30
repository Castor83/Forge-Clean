import re
import string


def normalize_agent_name(name: str) -> str:
    """
    Normalize agent names according to specified rules.
    
    Args:
        name: The agent name to normalize
        
    Returns:
        str: The normalized agent name
        
    Raises:
        ValueError: If input is not a string or result would be empty
    """
    # Rule 1: Other input types than str raise ValueError; message contains 'name'
    if not isinstance(name, str):
        raise ValueError("name")
    
    # Rule 2: Remove leading and trailing whitespace
    name = name.strip()
    
    # Rule 3: Convert ASCII letters to lowercase
    name = name.lower()
    
    # Rule 4: Replace sequences of non-alphanumeric characters with single hyphen
    name = re.sub(r'[^a-z0-9]+', '-', name)
    
    # Rule 5: Remove leading and trailing hyphens
    name = name.strip('-')
    
    # Rule 6: If result is empty, raise ValueError; message contains 'name'
    if not name:
        raise ValueError("name")
    
    # Rule 7: Function doesn't modify external state (implicit)
    
    return name