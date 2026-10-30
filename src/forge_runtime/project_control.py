"""Explicit host commands remove selected project data without adding rights."""

import copy

from .project_context import validate_project_context
from .project_session import begin_project_change, context_binding, _save_context_transaction


def remove_project_record(task, field, key):
    """Remove every revision of one rule/decision and discard old dialogue.

    This is scoped to the selected project record and the current conversation.
    Historical files, evidence and other project records are preserved.
    """
    context = begin_project_change(task)
    if context is None:
        raise ValueError("Zuerst mit /projekt_anlegen einen Projektkontext anlegen.")
    if field not in {"rules", "decisions"}:
        raise ValueError("Nur eine Projektregel oder Entscheidung kann entfernt werden.")
    identifier = "id" if field == "rules" else "key"
    if type(key) is not str or not any(record[identifier] == key for record in context[field]):
        raise ValueError("Diese Regel oder Entscheidung ist im Projektstand nicht gespeichert.")
    context = copy.deepcopy(context)
    context[field] = [record for record in context[field] if record[identifier] != key]
    context["revision"] += 1
    validate_project_context(context)
    candidate = copy.deepcopy(task.data)
    candidate.update(state="clarifying", messages=[], questions=[], proposal=None, report=None,
                     context_binding=context_binding(context), confirmed_context_sha256=None,
                     confirmed_proposal_sha256=None)
    from .session_automation import reset_session_binding
    reset_session_binding(candidate)
    candidate["events"].append(
        "Projektregel oder Entscheidung entfernt; Gespräch und Bauvorschlag zurückgesetzt.")
    _save_context_transaction(task, context, candidate)
