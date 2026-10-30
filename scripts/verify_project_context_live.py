"""Two-process local-model acceptance for host-owned project context.

Run from the repository, after explicitly authorizing local model calls:
    python -B scripts/verify_project_context_live.py --prepare \
        --workspace workspaces/project_context_live --model forge-30b
    python -B scripts/verify_project_context_live.py --resume \
        --workspace workspaces/project_context_live --model forge-30b

The second command must run in a new process. --prepare requires a fresh child
of workspaces. --resume refers only to the saved current assignment; it does
not repeat its required outputs. Reports and model_dialogue.jsonl contain only this synthetic
scenario, actual role/content messages and model responses, never credentials
or HTTP headers. Syntax checking and --help are not evidence of live acceptance.
Both report files must say passed=true before claiming this scenario passed.
No model call happens when importing this module or requesting --help.
"""

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.forge_runtime.conversation_build import build_confirmed, check_completed
from src.forge_runtime.conversation_cli import handle_project_command, require_task_child
from src.forge_runtime.conversation_json import load_conversation_json
from src.forge_runtime.conversation_protocol import discuss, parse_proposal_task
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.project_context import _reject_links, effective_project_context
from src.forge_runtime.project_session import context_binding
from src.forge_runtime.task_runner import ForgeTaskSpec, FunctionCase, StaticCheck, verify_forge_task


PROJECT_ID = "live_context_greeting"
PREPARE_REPORT = "acceptance_project_prepare_report.json"
RESUME_REPORT = "acceptance_project_resume_report.json"
OLD_RULE = "Nur ein Text-Ergebnis; keine Dateien, kein Netzwerk und keine weiteren Aktionen."
NEW_RULE = "Genau eine kleine importfreie Funktion; nur Text zurückgeben, keine weiteren Aktionen."
NEXT_STEP = "Den aktuellen Guten-Tag-Begrüßer bauen und unabhängig prüfen."
OLD_OBJECTIVE = 'Begrüßer sagt exakt "Hallo, <Name>!"; leerer Name: "Bitte Namen eingeben."'
NEW_OBJECTIVE = (
    'Begrüßer für genau einen Textparameter: Bei jedem nichtleeren Namen exakt "Guten Tag, <Name>!", '
    'bei ausschließlich der leeren Zeichenkette exakt "Bitte Namen eingeben." zurückgeben. '
    'Den Namen unverändert einsetzen: kein Trimmen und kein Normalisieren. Namen aus Leerzeichen '
    'sind normale nichtleere Namen. Verbindliche Prüffälle: "Ada" -> "Guten Tag, Ada!"; '
    '"Bruno" -> "Guten Tag, Bruno!"; "" -> "Bitte Namen eingeben."; '
    '"   " -> "Guten Tag,    !"; " Ada " -> "Guten Tag,  Ada !". '
    'Genau eine kleine importfreie Pythonfunktion mit Parametern und einem return-Ausdruck. '
    'Nur den Text zurückgeben; keine weiteren Aktionen. Technischen Funktions- und Dateinamen '
    'innerhalb der bestehenden Hostgrenzen wählen. Diese Entscheidung ersetzt frühere Begrüßungsvorgaben.'
)
REQUIRED_NAMES = ("Ada", "Bruno", "", "   ", " Ada ")
INDEPENDENT_NAMES = REQUIRED_NAMES + ("Carla", "Özlem")


def expected_greeting(name, prefix):
    return prefix + name + "!" if name else "Bitte Namen eingeben."


def complete_wish(prefix):
    examples = "; ".join(json.dumps(name, ensure_ascii=False) + " -> " +
                          json.dumps(expected_greeting(name, prefix), ensure_ascii=False)
                          for name in REQUIRED_NAMES)
    return ("Bau mir genau eine kleine importfreie Pythonfunktion als Begrüßer. "
            "Eingabe ist ein Name als Text. Ausschließlich die leere Zeichenkette gilt als leer; "
            "dann genau 'Bitte Namen eingeben.' zurückgeben. Sonst genau " +
            json.dumps(prefix + "<Name>!", ensure_ascii=False) + ". "
            "Name unverändert einsetzen: kein Trimmen oder Normalisieren. "
            "Verbindliche Beispiele: " + examples + ". "
            "Diese aktuellen Ausgaben ersetzen alle früheren Begrüßungsvorgaben. "
            "Nur den Text zurückgeben, keine anderen Aktionen. Den technischen Funktions- "
            "und Dateinamen darfst du innerhalb der bestehenden Grenzen wählen.")


def selected_workspace(value, *, prepare):
    directory = Path(value)
    if not directory.is_absolute():
        directory = ROOT / directory
    directory = directory.absolute()
    base = ROOT / "workspaces"
    require_task_child(directory, base)
    _reject_links(directory)
    directory = directory.resolve(strict=False)
    require_task_child(directory, base.resolve(strict=False))
    if prepare:
        if directory.exists():
            raise ValueError("--prepare benötigt einen frischen Unterordner von workspaces.")
        directory.mkdir(parents=True)
    elif not directory.is_dir():
        raise ValueError("--resume benötigt den vorhandenen vorbereiteten Arbeitsordner.")
    return directory


def report_file(directory, name):
    path = directory / name
    _reject_links(path)
    if path.exists() and not path.is_file():
        raise ValueError("Abnahmebericht muss eine reguläre Datei sein.")
    return path


def host_command(task, command, report):
    if not handle_project_command(task, command, lambda line: print(line, flush=True)):
        raise ValueError("Abnahmescript verwendete einen unbekannten Hostbefehl.")
    report["host_commands"].append(command)


def proposal_has_requested_cases(proposal, prefix):
    spec = parse_proposal_task(proposal)
    names = set()
    for case in spec.checks[0].cases:
        if len(case.args) != 1 or type(case.args[0]) is not str:
            raise ValueError("Begrüßer benötigt genau einen Textparameter in jedem Prüffall.")
        name = case.args[0]
        if case.expected != expected_greeting(name, prefix) or type(case.expected) is not str:
            raise ValueError("Modellvorschlag enthält einen falschen verbindlichen Begrüßungsfall.")
        names.add(name)
    if not set(REQUIRED_NAMES).issubset(names):
        raise ValueError("Modellvorschlag lässt einen verbindlichen Namens-/Randfall aus.")
    return spec


def propose(task, planner, prefix, report, *, use_saved_project=False):
    """At most two discussions, each with the existing one-correction limit."""
    wish = ("Bitte baue jetzt den aktuell gespeicherten Projektauftrag. Nutze die aktuelle Entscheidung; "
            "ältere Begrüßungsvorgaben wurden ersetzt.") if use_saved_project else complete_wish(prefix)
    for attempt in range(2):
        if attempt == 0:
            message = wish
        elif use_saved_project:
            message = ("Die aktuelle gespeicherte Entscheidung enthält bereits sämtliche genauen Ausgaben "
                       "und Prüffälle. Nutze diesen Projektstand und nimm alle dort festgelegten Beispiele "
                       "in den Bauvorschlag auf. Keine weiteren Ausgaben erfinden; der ersetzte frühere "
                       "Auftrag gilt nicht mehr.")
        else:
            message = ("Die vollständigen verbindlichen Angaben folgen nochmals. Keine zusätzlichen "
                       "Ausgaben erfinden; sämtliche genannten Beispiele in den Vorschlag aufnehmen. " + wish)
        report.setdefault("proposal_user_messages", []).append(message)
        response = discuss(task, message, planner)
        print(json.dumps(response, ensure_ascii=False), flush=True)
        report["proposal_attempts"] += 1
        if task.data["state"] == "proposed":
            try:
                return proposal_has_requested_cases(task.data["proposal"], prefix)
            except ValueError as exc:
                report["proposal_feedback"].append(str(exc))
    raise ValueError("Nach zwei begrenzten Klärungen kein passender konkreter Bauvorschlag.")


def assert_current_context(context):
    effective = effective_project_context(context)
    if effective["project_id"] != PROJECT_ID:
        raise ValueError("Live-Abnahme gehört zu einem anderen Projekt.")
    decisions = {item["key"]: item["value"] for item in effective["decisions"]}
    rules = {item["id"]: item["text"] for item in effective["rules"]}
    tasks = {item["id"]: (item["status"], item["text"]) for item in effective["tasks"]}
    if (decisions.get("objective") != NEW_OBJECTIVE or rules.get("scope") != NEW_RULE
            or effective["next_step"] != NEXT_STEP
            or tasks.get("old_proposal") != ("done", "Früheren Hallo-Vorschlag geprüft und ersetzt.")
            or tasks.get("current_build") != ("open", "Aktuellen Guten-Tag-Begrüßer bauen.")):
        raise ValueError("Aktuelle Projektwerte entsprechen nicht der vorbereiteten Live-Abnahme.")
    for field, text_field, old_value in (("rules", "text", OLD_RULE),
                                        ("decisions", "value", OLD_OBJECTIVE)):
        if not any(item["status"] == "superseded" and item[text_field] == old_value
                   for item in context[field]):
            raise ValueError("Ersetzte frühere Projektwerte fehlen in der gespeicherten Historie.")
        if any(item["status"] != "active" or item[text_field] == old_value
               for item in effective[field]):
            raise ValueError("Alter Projektwert wurde als aktueller Kontext übernommen.")
    return effective


def assert_actual_envelope(messages, expected, *, build_proposal=None, binding=None):
    """Check what reaches the client, after the host adds the context envelope."""
    context_index = len(messages) - 2
    if len(messages) < 3 or messages[0][0] != "system" or messages[context_index][0] != "user":
        raise ValueError("Pflichtkontext fehlt vor dem ersten Modellaufruf.")
    envelope = load_conversation_json(messages[context_index][1])
    if build_proposal is not None:
        from src.forge_runtime.conversation_store import proposal_digest
        if (type(envelope) is not dict or set(envelope) != {"kind", "source", "project_binding", "active_rules",
                             "confirmed_proposal", "confirmed_proposal_sha256"}
                or envelope["kind"] != "build_context_data"
                or envelope["source"] != "host_confirmed_build"
                or envelope["project_binding"] != binding
                or envelope["active_rules"] != expected["rules"]
                or envelope["confirmed_proposal"] != build_proposal
                or envelope["confirmed_proposal_sha256"] != proposal_digest(build_proposal)):
            raise ValueError("Tatsächlicher Baukontext entspricht nicht dem bestätigten Bauvertrag.")
        return
    if (type(envelope) is not dict or envelope.get("kind") != "project_context_data"
            or envelope.get("source") != "host_loaded_project"
            or envelope.get("context") != expected):
        raise ValueError("Tatsächliche Modellnachrichten enthalten nicht den aktuellen Hostkontext.")
    # Superseded entries stay on disk. Older conversation messages are history;
    # this check concerns the explicitly identified current project-data envelope.
    for field in ("rules", "decisions"):
        if any(item["status"] != "active" for item in envelope["context"][field]):
            raise ValueError("Historische Projektrevision ist im aktuellen Modellkontext aktiv.")


def counted_client(client, kind, directory, report, *, expected_context=None,
                   build_proposal=None, binding=None):
    """Log actual client input, preserving the host's role-specific tool schema."""
    path = directory / "model_dialogue.jsonl"
    _reject_links(path)
    if path.exists() and not path.is_file():
        raise ValueError("Modellprotokoll muss eine reguläre Datei sein.")

    def call(messages):
        if expected_context is not None:
            assert_actual_envelope(messages, expected_context, build_proposal=build_proposal, binding=binding)
            if kind == "conversation" and report["calls"][kind] == 0:
                report["autoload_before_first_call"] = {
                    "passed": True, "actual_context": copy.deepcopy(expected_context),
                    "context_role": messages[-2][0],
                    "superseded_values_in_current_context": False,
                }
        report["calls"][kind] += 1
        entry = {"phase": report["phase"], "process_id": os.getpid(),
                 "kind": kind, "messages": messages}
        try:
            response = client(messages)
            entry["response"] = response
            return response
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _reject_links(path)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")

    configure = getattr(client, "set_tool_actions", None)
    if configure is not None:
        call.set_tool_actions = configure
    return call


def prepare(directory, planner, report):
    with TaskStore(directory).exclusive() as store:
        task = ConversationTask(store)
        for command in (
            "/projekt_anlegen " + PROJECT_ID,
            "/entscheidung ziel " + OLD_OBJECTIVE,
            "/regel scope " + OLD_RULE,
            "/aufgabe old_proposal offen Früheren Hallo-Vorschlag klären.",
            "/naechster Den früheren Hallo-Vorschlag bestätigen.",
        ):
            host_command(task, command, report)
        propose(task, planner, "Hallo, ", report)
        task.confirm()
        if not task.is_confirmed():
            raise ValueError("Erster konkreter Vorschlag wurde nicht an den Projektstand gebunden.")
        report["old_confirmation"] = {
            "state": task.data["state"], "confirmed_proposal_sha256": task.data["confirmed_proposal_sha256"],
            "confirmed_context_sha256": task.data["confirmed_context_sha256"],
            "binding": copy.deepcopy(task.data["context_binding"]),
        }
        host_command(task, "/entscheidung ziel " + NEW_OBJECTIVE, report)
        revoked = (not task.is_confirmed() and task.data["state"] == "clarifying"
                   and task.data["proposal"] is None
                   and task.data["confirmed_proposal_sha256"] is None
                   and task.data["confirmed_context_sha256"] is None)
        report["old_confirmation_revoked"] = revoked
        if not revoked:
            raise ValueError("Aktuelle Entscheidung hat die frühere Bestätigung nicht aufgehoben.")
        blocked_calls = []
        try:
            build_confirmed(task, lambda messages: blocked_calls.append(messages), lambda _: None)
        except ValueError:
            pass
        else:
            raise ValueError("Früherer Vorschlag war nach Projektänderung weiterhin baubar.")
        report["unconfirmed_build_client_calls"] = len(blocked_calls)
        if blocked_calls:
            raise ValueError("Veralteter Bau hat trotz aufgehobener Bestätigung das Modell erreicht.")
        for command in (
            "/regel scope " + NEW_RULE,
            "/aufgabe old_proposal erledigt Früheren Hallo-Vorschlag geprüft und ersetzt.",
            "/aufgabe current_build offen Aktuellen Guten-Tag-Begrüßer bauen.",
            "/naechster " + NEXT_STEP,
        ):
            host_command(task, command, report)
        context = task.project_data()
        report["effective_current_context"] = assert_current_context(context)
        report["prepared_binding"] = context_binding(context)
        report["prepared_state"] = task.data["state"]
    report["passed"] = True


def read_prepare_report(directory, model):
    with report_file(directory, PREPARE_REPORT).open("rb") as stream:
        raw = stream.read(100_001)
    if len(raw) > 100_000:
        raise ValueError("Vorbereitungsbericht überschreitet das Größenlimit.")
    report = load_conversation_json(raw.decode("utf-8"))
    if (type(report) is not dict or report.get("passed") is not True
            or report.get("phase") != "prepare" or report.get("model") != model
            or report.get("project_id") != PROJECT_ID
            or report.get("old_confirmation_revoked") is not True
            or report.get("unconfirmed_build_client_calls") != 0):
        raise ValueError("Erfolgreicher passender Vorbereitungsbericht fehlt.")
    if type(report.get("process_id")) is not int or report["process_id"] == os.getpid():
        raise ValueError("--resume muss in einem neuen Prozess ausgeführt werden.")
    return report


def resume(directory, planner_client, builder_client, report):
    prepared = read_prepare_report(directory, report["model"])
    report["prepare_process_id"] = prepared["process_id"]
    report["new_process_verified"] = prepared["process_id"] != os.getpid()
    with TaskStore(directory).exclusive() as store:
        task = ConversationTask(store)
        # ConversationTask already autoloads and verifies its persisted binding.
        context = task.project_data()
        expected = assert_current_context(context)
        if context_binding(context) != prepared.get("prepared_binding"):
            raise ValueError("Projektstand wurde seit der kontrollierten Vorbereitung verändert.")
        if task.data["state"] != "clarifying" or task.is_confirmed():
            raise ValueError("Neuer Prozess hat eine frühere Bestätigung wiederverwendet.")
        report["autoload_binding"] = context_binding(context)
        planner = counted_client(planner_client, "conversation", directory, report,
                                 expected_context=expected)
        spec = propose(task, planner, "Guten Tag, ", report, use_saved_project=True)
        task.confirm()
        if not task.is_confirmed():
            raise ValueError("Aktueller Vorschlag ist nicht an den geladenen Projektstand gebunden.")
        report["new_confirmation"] = {
            "proposal_sha256": task.data["confirmed_proposal_sha256"],
            "context_sha256": task.data["confirmed_context_sha256"],
        }
        builder = counted_client(builder_client, "build", directory, report,
                                 expected_context=expected, build_proposal=copy.deepcopy(task.data["proposal"]),
                                 binding=context_binding(context))
        if not build_confirmed(task, builder, lambda line: print(line, flush=True)):
            raise ValueError("Aktueller Live-Bau erhielt keine Host-/Rollenfreigabe.")
        independent = ForgeTaskSpec(spec.task, (StaticCheck(
            kind="python_function_cases", path=spec.task.allowed_files[0],
            function=spec.checks[0].function,
            cases=tuple(FunctionCase((name,), expected_greeting(name, "Guten Tag, "))
                        for name in INDEPENDENT_NAMES)),))
        passed, detail = verify_forge_task(directory / "build", independent)
        report.update(intent_cases_passed=passed, intent_detail=detail,
                      independent_cases=[{"args": [name], "expected": expected_greeting(name, "Guten Tag, ")}
                                         for name in INDEPENDENT_NAMES],
                      pipeline_report=copy.deepcopy(task.data["report"]))
        if not passed:
            task.change(state="failed", report=dict(task.data["report"], approved=False,
                        intent_cases_passed=False, intent_detail=detail),
                        event="Unabhängige aktuelle Benutzerfälle fehlgeschlagen; keine Live-Abnahme.")
            raise ValueError("Unabhängige Guten-Tag-Fälle fehlgeschlagen: " + detail)
        pipeline = task.data["report"]
        if (pipeline.get("host_passed") is not True or pipeline.get("pipeline_approved") is not True
                or pipeline.get("approved") is not True
                or {item["role"] for item in pipeline.get("results", [])}
                != {"builder", "tester", "reviewer"}
                or any(item.get("passed") is not True for item in pipeline["results"])):
            raise ValueError("Live-Abnahme benötigt alle bestehenden Host-/Rollenfreigaben.")
    # Reopen the saved result through a fresh store and the real host checks.
    with TaskStore(directory).exclusive() as store:
        reopened = ConversationTask(store)
        check_completed(reopened)
        if reopened.data["state"] != "completed":
            raise ValueError("Unabhängige Wiederöffnung hat den Abschluss nicht bestätigt.")
        report["reopened_completion_verified"] = True
        report["reopened_binding"] = copy.deepcopy(reopened.data["context_binding"])
        report["files"] = copy.deepcopy(reopened.data["report"]["files"])
    report["passed"] = True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Projektstand mit lokalem Modell in zwei Prozessen abnehmen.",
        epilog="--prepare und danach --resume getrennt ausführen. --help/Syntax allein sind keine Live-Abnahme.")
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument("--prepare", action="store_true", help="Frisches Projekt vorbereiten und alte Bestätigung widerrufen.")
    phase.add_argument("--resume", action="store_true", help="In neuem Prozess aktuellen Projektstand laden und bauen.")
    parser.add_argument("--workspace", required=True, help="Frischer bzw. vorbereiteter Unterordner von workspaces.")
    parser.add_argument("--model", default="forge-30b")
    args = parser.parse_args(argv)
    mode = "prepare" if args.prepare else "resume"
    report = {"phase": mode, "project_id": PROJECT_ID, "model": args.model,
              "process_id": os.getpid(), "passed": False,
              "calls": {"conversation": 0, "build": 0}, "host_commands": [],
              "proposal_attempts": 0, "proposal_feedback": []}
    started = time.monotonic()
    path = None
    try:
        directory = selected_workspace(args.workspace, prepare=args.prepare)
        path = report_file(directory, PREPARE_REPORT if args.prepare else RESUME_REPORT)
        if path.exists():
            path = None
            raise ValueError("Abnahmebericht existiert bereits; frische Abnahme nicht überschreiben.")
        planner_client = LMStudioChatClient(args.model, timeout_seconds=300, response_kind="conversation")
        if args.prepare:
            planner = counted_client(planner_client, "conversation", directory, report)
            prepare(directory, planner, report)
        else:
            builder_client = LMStudioChatClient(args.model, timeout_seconds=300)
            resume(directory, planner_client, builder_client, report)
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if path is not None:
            _reject_links(path)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
