"""Local-model dialogue matrix; no generated code is executed or built."""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.lm_studio_executor import LMStudioChatClient


CASES = (
    ("typo_ambiguous", (("Ich brauch nen prüfa fur texte", "clarifying", ()),)),
    ("clear_greeting", ((
        'Bau einen Begrüßer: mit dem Namen als Eingabe soll exakt "Moin <Name>!" '
        'herauskommen, auch für leeren Namen: "Moin !". Nur Text zurückgeben.',
        "proposed", (("Ada", "Moin Ada!"), ("", "Moin !"))),)),
    ("contradictory_output", ((
        'Baue einen Helfer, der zu jedem Namen immer "Hallo!" zurückgibt. '
        'Die Ausgabe soll für jeden Namen immer genau "Tschüss!" sein. '
        'Beides ist gleich wichtig.', "clarifying", ()),)),
    ("unsupported_file_operation", ((
        "Sortiere meine Dateien im Downloadsordner automatisch nach Dateityp.", "clarifying", ()),)),
    ("scope_change", (
        ('Bau einen Begrüßer. Ausgabe genau "Hallo, <Name>!", bei leerem Namen "Kein Name.".',
         "proposed", (("Ada", "Hallo, Ada!"), ("", "Kein Name."))),
        ('Ändere nur die Begrüßung zu "Guten Tag, <Name>!". Die Meldung bei leerem Namen bleibt.',
         "proposed", (("Ada", "Guten Tag, Ada!"), ("", "Kein Name."))))),
    ("contextual_answer", (
        ("Ich will wissen ob Texte gültig sind.", "clarifying", ()),
        ("Nur wenn wenigstens ein Zeichen übrig bleibt, wenn man Leerzeichen am Anfang "
         "und Ende wegnimmt. Dann True, sonst False. Leerer Text ist False.",
         "proposed", (("", False), ("   ", False), ("x", True))))),
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--model", default="forge-30b")
    args = parser.parse_args(argv)
    directory = Path(args.workspace).resolve()
    directory.relative_to((ROOT / "workspaces").resolve())
    directory.mkdir(parents=True, exist_ok=False)
    client = LMStudioChatClient(args.model, timeout_seconds=300, response_kind="conversation")
    report = {"model": args.model, "cases": [], "passed": False}
    started = time.monotonic()
    for name, turns in CASES:
        folder = directory / name
        folder.mkdir()
        case = {"name": name, "passed": False, "turns": []}
        try:
            with TaskStore(folder).exclusive() as store:
                task = ConversationTask(store)
                for message, state, expected in turns:
                    response = discuss(task, message, client)
                    case["turns"].append({"message": message, "response": response, "state": task.data["state"]})
                    if task.data["state"] != state:
                        raise ValueError("Expected " + state + ", got " + task.data["state"])
                    if name == "unsupported_file_operation":
                        reply = response["reply"].lower()
                        if "nicht" not in reply or "datei" not in reply:
                            raise ValueError("Unsupported file action requires an explicit limitation in the reply")
                        questions = " ".join(response["questions"]).lower()
                        if any(word in questions for word in ("verschoben", "verschieben", "umbenennen", "neue ordner", "namen ändern")):
                            raise ValueError("Clarification must concern a supported text building block")
                    if expected:
                        checks = response["task"]["checks"][0]["cases"]
                        observed = {json.dumps(item["args"]): item["expected"] for item in checks}
                        # The model can choose test names; check the generic wording too.
                        objective = response["task"]["objective"]
                        for value, wanted in expected:
                            key = json.dumps([value])
                            if key in observed and (type(observed[key]) is not type(wanted) or observed[key] != wanted):
                                raise ValueError("Explicit test output conflicts with current user instruction")
                            if value == "" and key not in observed:
                                raise ValueError("Explicitly requested empty-input case is missing")
                        for item in checks:
                            if len(item["args"]) != 1 or type(item["args"][0]) is not str:
                                continue
                            value = item["args"][0]
                            if name == "clear_greeting":
                                wanted = "Moin " + value + "!"
                            elif name == "scope_change":
                                prefix = "Guten Tag, " if "Guten Tag" in message else "Hallo, "
                                wanted = prefix + value + "!" if value else "Kein Name."
                            elif name == "contextual_answer":
                                wanted = bool(value.strip())
                            else:
                                continue
                            if type(item["expected"]) is not type(wanted) or item["expected"] != wanted:
                                raise ValueError("Model test cases conflict with the exact user intent")
                        if name == "scope_change" and "Guten Tag" in message and "Guten Tag" not in objective:
                            raise ValueError("Changed greeting is missing from the new objective")
                        if name == "scope_change" and ("inklusive nur leerzeichen" in objective.lower() or "getrimmt" in objective.lower()):
                            raise ValueError("Whitespace trimming was not authorized by the user")
                case["passed"] = True
        except Exception as exc:
            case["error"] = f"{type(exc).__name__}: {exc}"
        report["cases"].append(case)
        print(json.dumps(case, ensure_ascii=False), flush=True)
        (directory / "dialogue_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["passed"] = all(case["passed"] for case in report["cases"])
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    (directory / "dialogue_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
