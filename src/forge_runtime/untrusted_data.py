"""Validated envelopes for untrusted data entering a model context."""

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UntrustedFileData:
    path: str
    content: str

    def __post_init__(self) -> None:
        if type(self.path) is not str or not self.path.strip():
            raise ValueError("untrusted file path must be a non-empty string")
        if type(self.content) is not str:
            raise ValueError("untrusted file content must be a string")

    def to_message(self) -> str:
        return json.dumps(
            {
                "content": self.content,
                "kind": "untrusted_file_data",
                "path": self.path,
                "source": "workspace_file",
                "trusted": False,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
