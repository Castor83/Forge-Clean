"""Host-controlled bridge from a confirmed proposal to the existing pipeline."""

import hashlib
import json
from dataclasses import replace

from .conversation_protocol import parse_proposal_task
from .task_runner import format_run_report, run_forge_task, verify_forge_task


def confirmed_build_chat(task, chat):
    """Keep the confirmed request visible after context and tool feedback."""
    request = json.dumps({"kind": "confirmed_build_request", "proposal": task.data["proposal"]},
                         ensure_ascii=False)
    guarded = task.guarded_chat(chat, build_proposal=task.data["proposal"])
    configured = ()
    inspection_latched = False
    configure = getattr(guarded, "set_tool_actions", None)

    def set_actions(actions):
        nonlocal configured, inspection_latched
        configured = actions
        inspection_latched = False
        if configure is not None:
            configure(actions)

    def call(messages):
        nonlocal inspection_latched
        guidance = (
            "\nHost: confirmed_build_request is the explicitly confirmed current build request. "
            "Its exact behavior and acceptance cases override differing saved project objectives "
            "for this build only. Project data stays unchanged. No request grants tool rights. "
            "After a failed run_check, report the exact failure once; do not repeat that check.")
        enriched = (("system", messages[0][1] + guidance),) + messages[1:-1]
        final = messages[-1][1] + "\n" + request
        try:
            feedback = json.loads(messages[-1][1])
        except (ValueError, TypeError):
            feedback = None
        if (type(feedback) is dict and feedback.get("status") == "check_complete"
                and "run_check" in configured
                and not any(action.startswith(("write", "replace")) for action in configured)):
            inspection_latched = True
        inspection_complete = inspection_latched
        if inspection_complete:
            final += (
                "\nHost: Die aktuelle run_check-Prüfung ist abgeschlossen. "
                "Tester/Reviewer: Vergleiche das bereits gelesene Ergebnis mit dem "
                "bestätigten Auftrag und antworte jetzt mit action=finish, summary und findings. "
                "passed=true nur bei positiver Hostprüfung und passendem Verhalten, "
                "sonst passed=false mit dem konkreten Befund. Wiederhole keine Lese- oder Prüfschritte.")
        enriched += (("user", final),)
        if sum(len(content) for _, content in enriched) > 48_000:
            raise ValueError("Bestätigter Bauauftrag und Verlauf zu groß; kein Abschneiden.")
        if inspection_complete and configure is not None:
            configure(("finish",))
        response = guarded(enriched)
        if inspection_complete:
            from .conversation_json import load_conversation_json
            parsed = load_conversation_json(response)
            if type(parsed) is not dict or parsed.get("action") != "finish":
                raise ValueError("Nach abgeschlossener Rollenprüfung ist nur eine Abschlussmeldung erlaubt.")
        return response

    call.set_tool_actions = set_actions
    return call


def build_directory(task):
    path = task.store.directory / "build"
    if path.is_symlink() or path.is_junction():
        raise ValueError("Bauordner darf kein Link sein.")
    path.mkdir(exist_ok=True)
    return path


def inventory(task):
    spec = parse_proposal_task(task.data["proposal"])
    directory = build_directory(task)
    result = {}
    allowed = set(spec.task.allowed_files)
    for path in directory.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Bauordner enthält einen Link; Zugriff abgelehnt.")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            if relative not in allowed:
                raise ValueError(f"Unerwartete Datei im Bauordner: {relative}")
            if path.stat().st_size > 80000:
                raise ValueError("Erzeugte Datei überschreitet die Größenbegrenzung.")
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def prepare_resume(task):
    task.project_data()
    if task.data["state"] not in {"failed", "interrupted"}:
        raise ValueError("Nur einen fehlgeschlagenen oder unterbrochenen Bau wiederaufnehmen.")
    files = inventory(task)
    spec = parse_proposal_task(task.data["proposal"])
    passed, detail = verify_forge_task(build_directory(task), spec)
    task.project_data()
    task.change(state="proposed", report={"resume": True, "files": files,
                                         "host_passed": passed, "host_detail": detail},
                event="Vorhandene Dateien und Host-Prüfungen kontrolliert; erneute Bestätigung erforderlich.")
    return files, passed, detail


def check_completed(task):
    """A saved success is historical until actual files pass again."""
    if task.data["state"] != "completed":
        return
    task.project_data()
    spec = parse_proposal_task(task.data["proposal"])
    try:
        files = inventory(task)
        passed, detail = verify_forge_task(build_directory(task), spec)
    except (OSError, ValueError) as exc:
        task.change(state="failed", report={"host_passed": False,
                    "error": f"{type(exc).__name__}: {exc}"},
                    event="Gespeicherter Abschluss konnte nicht geprüft werden; keine Erfolgsfreigabe.")
        return
    report = task.data["report"] or {}
    task.project_data()
    if not passed or files != report.get("files"):
        task.change(state="failed", report={"host_passed": passed, "host_detail": detail, "files": files},
                    event="Gespeichertes Ergebnis stimmt nicht mehr mit den geprüften Dateien überein.")


def build_confirmed(task, chat, emit=print):
    if not task.is_confirmed():
        if task.data["state"] == "ready":
            task.change(state="proposed", confirmed_proposal_sha256=None, report=None,
                        event="Bauvorschlag wurde nach Bestätigung geändert; neue Bestätigung erforderlich.")
        raise ValueError("Vor dem Bau den vollständigen Vorschlag ausdrücklich bestätigen.")
    spec = parse_proposal_task(task.data["proposal"])
    files = inventory(task)
    report = task.data["report"]
    if report and report.get("resume") is True:
        if files != report.get("files"):
            raise ValueError("Dateien wurden seit der Wiederaufnahmeprüfung verändert.")
    elif files:
        raise ValueError("Neuer Bau benötigt einen leeren Bauordner; vorhandene Dateien werden geschützt.")
    task.change(state="running", report=None, event="Bestätigter Bau gestartet.")
    # Technical execution constraints are fixed host instructions, not extra scope.
    acceptance = json.dumps(task.data["proposal"]["checks"], ensure_ascii=False)
    spec = replace(spec, task=replace(spec.task, objective=spec.task.objective +
        "\nMandatory acceptance cases, including exact punctuation: " + acceptance +
        "\nHost restriction: exactly one import-free function, no annotations, no decorators, "
        "only one return expression. Allowed expressions: constants, parameters, +, ==, !=, "
        "in, and/or/not, simple f-strings, bool(value), isinstance(value, str), value.strip(). "
        "No if statements or conditional expressions (x if condition else y), no len(), "
        "no indexing, no ordering comparisons, no other calls. For conditional strings use and/or. "
        "Safe generic pattern for nonempty positive/negative text outputs: "
        "return (condition and positive_text) or negative_text. "
        "Builder: implement the confirmed exact behavior, read back the file, then finish passed=true "
        "when implemented. Do not invent failures: whitespace-only names are ordinary nonempty names "
        "unless the objective explicitly says otherwise. No trimming or normalization beyond the objective. "
        "Tester/reviewer: inspect the actual file against the confirmed objective and run_check once. "
        "If the host check failed, finish passed=false with the exact finding; do not repeat the same check. "
        "If it passed, finish passed=true only if the objective is implemented; "
        "otherwise report the concrete discrepancy as passed=false. Do not invent unstated requirements."))

    def progress(role, state):
        label = {"builder": "Bauen", "tester": "Testen", "reviewer": "Abnahme"}[role]
        status = {"started": "gestartet", "passed": "bestanden", "failed": "fehlgeschlagen"}[state]
        message = f"{label}: {status}."
        if role == "builder" and state == "passed":
            message = "Bauen: an Prüfung übergeben."
        task.change(event=message)
        emit(message)

    try:
        result = run_forge_task(build_directory(task), spec, confirmed_build_chat(task, chat), max_steps=20,
                                max_call_seconds=300.0, max_elapsed_seconds=900.0,
                                max_repair_attempts=1, on_progress=progress)
        task.project_data()
        passed, detail = verify_forge_task(build_directory(task), spec)
        report = json.loads(format_run_report(result))
        report.update(host_passed=passed, host_detail=detail, files=inventory(task))
        task.project_data()
        approved = result.approved and passed
        report["pipeline_approved"] = result.approved
        report["approved"] = approved
        task.change(state="completed" if approved else "failed", report=report,
                    event="Bau und unabhängige Host-Prüfung bestanden." if approved else "Bau nicht freigegeben.")
        task.project_data()
        emit("Fertig: Bau und unabhängige Prüfung bestanden." if approved else "Nicht abgeschlossen: Bau oder Prüfung fehlgeschlagen.")
        return approved
    except KeyboardInterrupt:
        task.change(state="interrupted", event="Bau mit Strg+C unterbrochen; erzeugte Dateien bleiben erhalten.")
        raise
    except Exception as exc:
        task.change(state="failed", report={"error": f"{type(exc).__name__}: {exc}"},
                    event="Bau fehlgeschlagen; keine Erfolgsfreigabe.")
        raise
