"""Small German terminal conversation, with explicit host commands."""

import argparse
import json
from pathlib import Path

from .conversation_build import build_confirmed, check_completed, prepare_resume
from .conversation_protocol import discuss, parse_proposal_task
from .conversation_store import ConversationTask, TaskStore
from .lm_studio_executor import LMStudioChatClient
from .project_context import effective_project_context
from .project_control import remove_project_record
from .project_inspection import inspect_project_store
from .project_session import begin_project_change, recover_project_store, update_project
from .tool_loop import ToolLoopDeadlineExceeded


LABELS = {"clarifying": "Klärung nötig", "proposed": "Vorschlag zur Prüfung",
          "ready": "Bereit zum Bau", "running": "Bau läuft", "completed": "Abgeschlossen",
          "failed": "Fehlgeschlagen", "cancelled": "Abgebrochen", "interrupted": "Unterbrochen"}
HELP = ("Schreibe deinen Wunsch oder eine Antwort. Befehle: /status, /auftrag, /bericht, /bestaetigen, "
        "/bauen, /wiederaufnahme, /projekt, /projekt_anlegen, /regel, /entscheidung, /aufgabe, "
        "/regel_entfernen, /entscheidung_entfernen, /naechster, /projekt_pruefen, "
        "/abbrechen, /ende. Während eines Laufs: Strg+C.")


def require_task_child(directory, base):
    try:
        directory.relative_to(base)
    except ValueError as exc:
        raise ValueError("Arbeitsordner muss innerhalb von workspaces liegen.") from exc
    if directory == base:
        raise ValueError("Eigenen Unterordner unter workspaces verwenden.")


def show_status(task, emit=print):
    context = task.project_data()
    data = task.data
    emit("Zustand: " + LABELS[data["state"]])
    if context is not None:
        emit("Projekt: " + context["project_id"])
        emit("Geplanter nächster Schritt: " + context["next_step"])
    for question in data["questions"]:
        emit("Offene Frage: " + question)
    if data["proposal"] is not None:
        spec = parse_proposal_task(data["proposal"])
        emit("Ziel: " + spec.task.objective)
        emit("Dateien im Bauordner: " + ", ".join(spec.task.allowed_files))
        emit("Prüffälle (Eingabe → erwartete Ausgabe):")
        for case in spec.checks[0].cases:
            emit(json.dumps(list(case.args), ensure_ascii=False) + " → " + json.dumps(case.expected, ensure_ascii=False))
    if data["events"]:
        emit("Letzter Fortschritt: " + data["events"][-1])
    if data["report"] is not None:
        report = data["report"]
        if "host_passed" in report:
            emit("Unabhängige Prüfung: " + ("bestanden" if report["host_passed"] else "nicht bestanden"))
        if "intent_cases_passed" in report:
            emit("Zusätzliche Benutzerfälle: " + ("bestanden" if report["intent_cases_passed"] else "nicht bestanden"))
            if not report["intent_cases_passed"] and "intent_detail" in report:
                emit("Abnahmefehler: " + str(report["intent_detail"]))
            if not report["intent_cases_passed"] and report.get("results"):
                emit("Frühere Rollenprüfungen:")
        for result in report.get("results", []):
            label = {"builder": "Bauen", "tester": "Testen", "reviewer": "Abnahme"}.get(result.get("role"), "Rolle")
            emit(label + ": " + ("freigegeben" if result.get("passed") else "nicht freigegeben"))
        if "error" in report:
            emit("Fehler: " + str(report["error"]))
        emit("Den vollständigen technischen Prüfbericht zeigt /bericht.")


def show_project(task, emit=print):
    data = task.project_data()
    if data is None:
        emit("Projektstand noch nicht eingerichtet. Nutze /projekt_anlegen <kurzer_name>.")
        return
    data = effective_project_context(data)
    emit("Projekt: " + data["project_id"] + "; gespeicherter Stand " + str(data["revision"]))
    for rule in data["rules"]:
        emit("Regel " + rule["id"] + ": " + rule["text"])
    labels = {"objective": "Ziel", "acceptance_notes": "Prüfkriterien", "working_notes": "Arbeitsweise"}
    for decision in data["decisions"]:
        emit(labels[decision["key"]] + ": " + decision["value"])
    for item in data["tasks"]:
        emit(("Offen: " if item["status"] == "open" else "Erledigt: ") + item["text"])
    emit("Nächster Schritt: " + data["next_step"])
    for item in data["evidence"]:
        emit("Historischer Nachweis: " + item["text"])


def handle_project_command(task, message, emit):
    parts = message.split(maxsplit=1)
    name = parts[0]
    if name in {"/projekt_anlegen", "/regel", "/entscheidung", "/aufgabe", "/naechster", "/nächster",
                "/regel_entfernen", "/entscheidung_entfernen"}:
        # A recognized change request invalidates old proposals before parsing.
        # Invalid syntax must not leave an earlier approval usable.
        begin_project_change(task)
    if name == "/projekt":
        if len(parts) != 1:
            raise ValueError("Nutze /projekt ohne weitere Angaben.")
        show_project(task, emit)
        return True
    if name == "/projekt_anlegen":
        if len(parts) != 2:
            raise ValueError("Nutze /projekt_anlegen <kurzer_name>.")
        task.initialize_project(parts[1].strip())
    elif name == "/projekt_pruefen":
        if len(parts) != 2:
            raise ValueError("Nutze /projekt_pruefen <projektkennung>; gültigen aktuellen Stand damit ausdrücklich übernehmen.")
        task.recover_project(parts[1].strip())
    elif name in {"/regel", "/entscheidung"}:
        values = message.split(maxsplit=2)
        if len(values) != 3:
            raise ValueError("Nutze " + name + " <kennung> <vollständiger Text>.")
        key = values[1]
        if name == "/entscheidung":
            names = {"ziel": "objective", "pruefkriterien": "acceptance_notes", "arbeitsweise": "working_notes"}
            if key not in names:
                raise ValueError("Entscheidung: ziel, pruefkriterien oder arbeitsweise wählen.")
            key = names[key]
        update_project(task, "rules" if name == "/regel" else "decisions", key, values[2])
    elif name in {"/regel_entfernen", "/entscheidung_entfernen"}:
        values = message.split()
        if len(values) != 2:
            raise ValueError("Nutze " + name + " <kennung>.")
        key = values[1]
        if name == "/entscheidung_entfernen":
            names = {"ziel": "objective", "pruefkriterien": "acceptance_notes", "arbeitsweise": "working_notes"}
            if key not in names:
                raise ValueError("Entscheidung: ziel, pruefkriterien oder arbeitsweise wählen.")
            key = names[key]
        remove_project_record(task, "rules" if name == "/regel_entfernen" else "decisions", key)
        emit("Alle gespeicherten Revisionen dieser Kennung sind aus dem Projektstand entfernt. "
             "Gespräch und Bauvorschlag dieses Auftrags sind zurückgesetzt. "
             "Historische Nachweise und andere Dateien bleiben erhalten.")
    elif name == "/aufgabe":
        values = message.split(maxsplit=3)
        if len(values) != 4 or values[2] not in {"offen", "erledigt"}:
            raise ValueError("Nutze /aufgabe <kennung> offen|erledigt <Beschreibung>.")
        update_project(task, "tasks", values[1], values[3],
                       status="open" if values[2] == "offen" else "done")
    elif name in {"/naechster", "/nächster"}:
        if len(parts) != 2:
            raise ValueError("Nutze /naechster <nächster konkreter Schritt>.")
        update_project(task, "next_step", None, parts[1])
    else:
        return False
    emit("Projektstand gespeichert. Frühere Bauvorschläge sind aufgehoben; aktuellen Wunsch neu klären.")
    show_project(task, emit)
    return True


def interact(task, planner, builder, read=input, emit=print, *,
             session_drafter=None, session_reviewer=None):
    session_drafter = session_drafter or getattr(task, "session_drafter", None)
    session_reviewer = session_reviewer or getattr(task, "session_reviewer", None)
    emit("Forge – lokaler Bauauftrag. Gespräch wird in diesem Arbeitsordner gespeichert.")
    emit(HELP)
    show_status(task, emit)
    while True:
        try:
            message = read("Du: ")
            command = message.strip()
            if command == "/ende":
                return 1 if task.store.retain_lock_on_close else 0
            if command == "/status":
                show_status(task, emit)
            elif command == "/auftrag":
                emit(json.dumps(task.data["proposal"], ensure_ascii=False, indent=2))
            elif command == "/bericht":
                emit(json.dumps(task.data["report"], ensure_ascii=False, indent=2))
            elif command in {"/bestaetigen", "/bestätigen"}:
                # Display complete proposal before changing host readiness.
                show_status(task, emit)
                parse_proposal_task(task.data["proposal"])
                task.confirm()
                emit("Vorschlag bestätigt. Mit /bauen beginnt die Umsetzung.")
            elif command == "/bauen":
                build_confirmed(task, builder, emit)
            elif command == "/wiederaufnahme":
                files, passed, detail = prepare_resume(task)
                emit("Vorhandene Dateien: " + json.dumps(files, ensure_ascii=False))
                emit("Aktuelle Host-Prüfung: " + str(passed) + "; " + detail)
                show_status(task, emit)
            elif command == "/abbrechen":
                task.cancel()
                emit("Auftrag abgebrochen.")
                return 0
            elif command.startswith("/") and handle_project_command(task, message, emit):
                pass
            elif command.startswith("/"):
                emit(HELP)
            elif command:
                from .session_automation import should_rotate, rotate_session
                if should_rotate(task, message):
                    emit("Das Gespräch nähert sich der bekannten Zeichengrenze. "
                         "Ich sichere den Verlauf und prüfe die Übergabe für einen neuen Gesprächsabschnitt. "
                         "Dies ist keine Messung der verbleibenden Modelltoken.")
                    rotate_session(task, message, session_drafter, session_reviewer, emit)
                emit("Ich prüfe deinen Wunsch und die bisherigen Antworten …")
                response = discuss(task, message, planner)
                emit("Forge: " + response["reply"])
                show_status(task, emit)
        except EOFError:
            return 1 if task.store.retain_lock_on_close else 0
        except KeyboardInterrupt:
            if task.store.retain_lock_on_close:
                emit("Speichern unterbrochen; Speicherstand prüfen. Alte Baubestätigung ist gesperrt.")
                emit("Sperrdatei bleibt erhalten; Prozess und gespeicherten Stand vor einer manuellen Freigabe prüfen.")
            else:
                emit("Unterbrochen. Gespeicherter Auftrag kann erneut geöffnet werden.")
            return 130
        except ToolLoopDeadlineExceeded as exc:
            emit("Die Modellantwort hat die Wartegrenze überschritten; keine Freigabe.")
            if exc.role.startswith("session-"):
                emit("Übergabe nicht aktiviert. Die neue Nachricht bleibt in der angezeigten Datei erhalten. "
                     "Der Modellaufruf kann noch auslaufen; Auftrag bleibt gesperrt und wird beendet.")
                return 1
            if task.data["state"] == "clarifying":
                emit("Dein Wunsch ist gespeichert. Du kannst die Nachricht erneut eingeben.")
            else:
                emit("Vor einem neuen Bau zuerst /wiederaufnahme verwenden.")
        except (OSError, RuntimeError, ValueError) as exc:
            emit("Nicht ausgeführt: " + str(exc))
            if task.store.retain_lock_on_close:
                emit("Speicherstand ungeklärt; Änderung gilt als nicht gespeichert. Alte Baubestätigung ist aufgehoben.")
                emit("Speicherfehler beheben und Wunsch erneut eingeben. Beim Beenden: Sperrdatei bleibt erhalten.")
                if task.store.project_uncertain:
                    emit("Projektdateien prüfen und gültige fehlende Dateien wiederherstellen. "
                         "Danach /projekt_pruefen <projektkennung> für die ausdrückliche Übernahme verwenden.")


def main(argv=None, *, chat_factory=None):
    parser = argparse.ArgumentParser(description="Mit Forge einen kleinen lokalen Bauauftrag besprechen.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--model", default="forge-30b")
    parser.add_argument("--endpoint", default="http://127.0.0.1:1234/v1/chat/completions")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--recover-project-id", help="Gültigen aktuellen Projektstand nach Speicherfehler ausdrücklich übernehmen.")
    mode.add_argument("--inspect-project", action="store_true",
                      help="Gespeicherte Berichte und Dateien ausschließlich lesen, auch bei gesperrtem Auftrag.")
    args = parser.parse_args(argv)
    try:
        base = Path(__file__).resolve().parents[2] / "workspaces"
        directory = Path(args.workspace).absolute()
        require_task_child(directory, base)
        for path in (directory, *directory.parents):
            if path == base.parent:
                break
            if path.is_symlink() or path.is_junction():
                raise ValueError("Arbeitsordner darf keine Links enthalten.")
        # Resolve parent segments and check the real boundary before mkdir.
        resolved_base = base.resolve(strict=False)
        directory = directory.resolve(strict=False)
        require_task_child(directory, resolved_base)
        if args.inspect_project:
            inspection = inspect_project_store(directory)
            print("Forge – lesende Inspektion des gespeicherten Auftrags.")
            print("Berichte und Dateien sind historische Beobachtungen, keine aktuelle Freigabe. "
                  "Getrennte Lesezugriffe ergeben keine zugesicherte gemeinsame Momentaufnahme.")
            if inspection["observations"]["lock"]["status"] != "missing":
                print("Sperrdatei vorhanden; sie bleibt erhalten.")
            print(json.dumps(inspection, ensure_ascii=True, indent=2))
            return 1 if inspection["problems"] else 0
        directory.mkdir(parents=True, exist_ok=True)
        require_task_child(directory.resolve(strict=True), base.resolve(strict=True))
        store = TaskStore(directory)
        factory = LMStudioChatClient if chat_factory is None else chat_factory
        if not callable(factory):
            raise ValueError("chat_factory must be callable")
        planner = factory(args.model, endpoint=args.endpoint, timeout_seconds=300, response_kind="conversation")
        builder = factory(args.model, endpoint=args.endpoint, timeout_seconds=300)
        session_drafter = factory(args.model, endpoint=args.endpoint, timeout_seconds=300, response_kind="handoff_draft")
        session_reviewer = factory(args.model, endpoint=args.endpoint, timeout_seconds=300, response_kind="handoff_review")
        with store.exclusive():
            if args.recover_project_id:
                recover_project_store(store, args.recover_project_id)
            task = ConversationTask(store)
            if task.data["proposal"] is not None:
                parse_proposal_task(task.data["proposal"])
            check_completed(task)
            task.session_drafter = session_drafter
            task.session_reviewer = session_reviewer
            return interact(task, planner, builder)
    except (OSError, RuntimeError, ValueError) as exc:
        print("Forge konnte nicht starten: " + str(exc))
        if "store" in locals() and store.retain_lock_on_close:
            print(store.lock_message())
            print("Bei einem Projektfehler zuerst die Projektdateien prüfen und gültige fehlende Dateien "
                  "wiederherstellen. Nach der Prozessprüfung und manuellen Sperrfreigabe diesen "
                  "Startbefehl um --recover-project-id <projektkennung> ergänzen. "
                  "Die Übernahme erfordert einen neuen Bauvorschlag und eine neue Bestätigung; "
                  "laufende oder beendete Aufträge werden dabei nicht übernommen.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
