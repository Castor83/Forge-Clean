import os
from pathlib import Path
from typing import List

def missing_required_files(root: Path) -> List[str]:
    """Returns a sorted list of relative paths to missing required files."""
    required_files = [
        "AGENTS.md",
        "PAUSE_STATE.md",
        "scripts/verify.ps1"
    ]
    
    missing = []
    for file_path in required_files:
        if not (root / file_path).is_file():
            missing.append(file_path)
    
    return sorted(missing)

def is_workspace_ready(root: Path) -> bool:
    """Returns True if all required files are present."""
    return len(missing_required_files(root)) == 0