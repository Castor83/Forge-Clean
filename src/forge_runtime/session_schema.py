"""Closed output schemas for isolated, tool-free session handoff roles."""


def text():
    return {"type": "string", "minLength": 1, "maxLength": 2000}


def texts(minimum=0):
    return {"type": "array", "items": text(), "minItems": minimum, "maxItems": 64}


def closed(properties):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


SESSION_DRAFT_SCHEMA = closed({
    "goal": text(), "rules": texts(1), "decisions": texts(),
    "completed": texts(), "open_tasks": texts(), "changed_files": texts(),
    "checks": texts(), "processes": texts(), "risks": texts(),
    "assumptions": texts(), "discarded_approaches": texts(),
    "next_step": closed({"action": text(), "prerequisites": texts()}),
})

SESSION_REVIEW_SCHEMA = closed({
    "checks": closed({name: closed({"status": {"type": "string", "enum": ["pass", "fail", "unverified"]},
                                    "note": text()})
                      for name in ("completeness", "source_consistency", "tasks_and_decisions",
                                   "current_instructions", "next_step", "limitations")}),
    "findings": texts(), "limitations": texts(1),
})
