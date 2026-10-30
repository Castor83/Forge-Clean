"""Unambiguous JSON input for local conversations and persisted task state."""

import json
import math


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON enthält ein mehrfach angegebenes Feld: " + key)
        result[key] = value
    return result


def _constant(value):
    raise ValueError("JSON enthält eine nicht endliche Zahl: " + value)


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON enthält eine nicht endliche Zahl durch Zahlenüberlauf.")
    return number


def load_conversation_json(content):
    try:
        return json.loads(content, object_pairs_hook=_object, parse_constant=_constant,
                          parse_float=_float)
    except RecursionError as exc:
        raise ValueError("JSON ist zu tief verschachtelt; Antwort oder Auftragsdatei abgewiesen.") from exc
