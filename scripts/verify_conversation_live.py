"""Reproducible local-model acceptance with independent user-intent cases."""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.forge_runtime.conversation_build import build_confirmed, check_completed, prepare_resume
from src.forge_runtime.conversation_protocol import discuss, parse_proposal_task
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.task_runner import ForgeTaskSpec, FunctionCase, StaticCheck, verify_forge_task


SCENARIOS = {
    "greeting": (
        "Ich brauch nen kleinen helfer der leute begrüst.",
        'Bei Ada soll genau "Hallo, Ada!" herauskommen. Bei Bruno genau "Hallo, Bruno!". '
        'Bei leerem Namen genau "Bitte Namen eingeben." (mit Punkt). Sonst "Hallo, <Name>!". '
        "Nur Text zurückgeben, nichts speichern und keine anderen Aktionen.",
        (("Ada", "Hallo, Ada!"), ("Bruno", "Hallo, Bruno!"), ("", "Bitte Namen eingeben."),
         ("Carla", "Hallo, Carla!"), ("Özlem", "Hallo, Özlem!"),
         ("   ", "Hallo,    !"), (" Ada ", "Hallo,  Ada !"))),
    "validator": (
        "Bau mir was das meine texte pruft.",
        "Gültig ist ein Text mit mindestens einem sichtbaren Zeichen. Dann True, sonst False. "
        "Leerer Text und nur Leerzeichen ergeben False. Nur prüfen, nichts speichern.",
        (("", False), ("abcd", True), ("  abc  ", True), ("     ", False)))
}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="greeting")
    parser.add_argument("--model", default="forge-30b")
    parser.add_argument("--direct", action="store_true", help="Start with the complete user request")
    parser.add_argument("--interrupt-after-write", action="store_true", help="Controlled KeyboardInterrupt after a real model write")
    parser.add_argument("--resume", action="store_true", help="Resume a previously controlled interrupted build in a new process")
    args = parser.parse_args(argv)
    directory = Path(args.workspace).resolve()
    directory.relative_to((ROOT / "workspaces").resolve())
    if directory.exists() and not args.resume:
        raise ValueError("Live-Abnahme benötigt einen frischen Arbeitsordner.")
    if not args.resume:
        directory.mkdir(parents=True)
    report = {"scenario": args.scenario, "model": args.model, "passed": False,
              "calls": {"conversation": 0, "build": 0}, "elapsed_seconds": None,
              "direct": args.direct, "resume": args.resume, "controlled_interrupt": args.interrupt_after_write}
    started = time.monotonic()
    interrupt_pending = False

    def counted(client, kind):
        def call(messages):
            nonlocal interrupt_pending
            if kind == "build" and interrupt_pending:
                raise KeyboardInterrupt
            report["calls"][kind] += 1
            result = client(messages)
            with (directory / "model_dialogue.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": kind, "messages": messages, "response": result}, ensure_ascii=False) + "\n")
            if kind == "build" and args.interrupt_after_write and json.loads(result).get("action") == "write_file":
                interrupt_pending = True
            return result
        return call

    planner = counted(LMStudioChatClient(args.model, timeout_seconds=300, response_kind="conversation"), "conversation")
    builder = counted(LMStudioChatClient(args.model, timeout_seconds=300), "build")
    try:
        with TaskStore(directory).exclusive() as store:
            task = ConversationTask(store)
            wish, clarification, expected = SCENARIOS[args.scenario]
            if args.resume:
                files, passed, detail = prepare_resume(task)
                report["resume_inspection"] = {"files": files, "host_passed": passed, "detail": detail}
            else:
                if not args.direct:
                    first = discuss(task, wish, planner)
                    print(json.dumps(first, ensure_ascii=False), flush=True)
                    if not first["questions"] or task.data["state"] != "clarifying":
                        raise ValueError("Unklarer Wunsch wurde ohne wesentliche Rückfrage als fertig behandelt.")
                second = discuss(task, "Bau mir einen kleinen Helfer. " + clarification if args.direct else clarification, planner)
                print(json.dumps(second, ensure_ascii=False), flush=True)
            if task.data["state"] != "proposed":
                raise ValueError("Vollständige Antwort führte nicht zum konkreten Bauvorschlag.")
            spec = parse_proposal_task(task.data["proposal"])
            task.confirm()
            try:
                if not build_confirmed(task, builder, lambda line: print(line, flush=True)):
                    raise ValueError("Live-Bau nicht freigegeben.")
            except KeyboardInterrupt:
                if not args.interrupt_after_write:
                    raise
                report["interrupted_state_verified"] = task.data["state"] == "interrupted"
                report["passed"] = report["interrupted_state_verified"]
                return 0 if report["passed"] else 1
            independent = ForgeTaskSpec(spec.task, (StaticCheck(
                kind="python_function_cases", path=spec.task.allowed_files[0],
                function=spec.checks[0].function,
                cases=tuple(FunctionCase((value,), wanted) for value, wanted in expected)),))
            passed, detail = verify_forge_task(directory / "build", independent)
            report.update(intent_cases_passed=passed, intent_detail=detail,
                          files=task.data["report"]["files"], pipeline_report=task.data["report"])
            if not passed:
                failed_report = dict(task.data["report"], approved=False,
                                     intent_cases_passed=False, intent_detail=detail)
                task.change(state="failed", report=failed_report,
                            event="Zusätzliche unabhängige Benutzerfälle fehlgeschlagen; keine Live-Abnahme.")
                raise ValueError("Unabhängige Fälle aus der Benutzerantwort fehlgeschlagen: " + detail)
        # New exclusive session independently verifies saved completion.
        with TaskStore(directory).exclusive() as store:
            task = ConversationTask(store)
            check_completed(task)
            if task.data["state"] != "completed":
                raise ValueError("Wiederöffnung hat Abschluss nicht bestätigt.")
        report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report_name = "acceptance_resume_report.json" if args.resume else "acceptance_report.json"
        (directory / report_name).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
