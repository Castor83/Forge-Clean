def is_valid_agent_identifier(identifier: object) -> bool:
    """
    Validates if the given identifier is a valid agent identifier.
    
    A valid identifier:
    1. Must be of type str
    2. Must be between 1 and 40 characters long
    3. Must consist only of lowercase letters a-z, digits 0-9, and single hyphens
    4. Must start with a letter a-z
    5. Must end with a letter a-z or digit 0-9
    6. Must not contain consecutive hyphens
    
    Args:
        identifier (object): The identifier to validate
        
    Returns:
        bool: True if the identifier is valid, False otherwise
    """
    # Check if it's a string
    if not isinstance(identifier, str):
        return False
    
    # Check length
    if not (1 <= len(identifier) <= 40):
        return False
    
    # Check if it starts with a letter a-z
    if not ('a' <= identifier[0] <= 'z'):
        return False
    
    # Check if it ends with a letter a-z or digit 0-9
    if not (('a' <= identifier[-1] <= 'z') or ('0' <= identifier[-1] <= '9')):
        return False
    
    # Check for consecutive hyphens
    if '--' in identifier:
        return False
    
    # Check allowed characters (lowercase letters a-z, digits 0-9, and hyphens only)
    for char in identifier:
        if not (('a' <= char <= 'z') or ('0' <= char <= '9') or char == '-'):
            return False
    
    return True