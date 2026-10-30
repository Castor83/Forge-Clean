"""Explicit local commands for the source-bound handoff core; no session start."""

import argparse
import hashlib
import json
from pathlib import Path

from .conversation_json import load_conversation_json
from .session_handoff import (
    bind_review, create_handoff, inspect_handoff, load_handoff,
    render_handoff, save_handoff,
)
from .session_handoff_io import MAX_PACKAGE_BYTES, checked_root, selected_path, read_regular


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _read_input(root, relative):
    # State/report files are chosen by the caller, never by stored narratives.
    root = checked_root(root)
    raw, _ = read_regular(selected_path(root, relative), MAX_PACKAGE_BYTES)
    return raw


def _json_input(root, relative):
    return load_conversation_json(_read_input(root, relative).decode("utf-8", errors="strict"))


def _parser():
    parser = argparse.ArgumentParser(description="Lokale Übergabe erstellen, prüfen und als Text ausgeben.")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT,
                        help="Ausdrücklich ausgewählte vorhandene Arbeitswurzel.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "review", "inspect", "export"):
        command = commands.add_parser(name)
        command.add_argument("--project", required=True, help="Erwartete Projektkennung.")
        command.add_argument("--adapter", required=True, help="Erwartete Agenten-/Adapterkennung.")
        command.add_argument("--source", action="append", required=True,
                             help="Ausgewählte Quelle; für jede Quelle wiederholen.")
        command.add_argument("--file", required=True, help="Relative Übergabedatei unter der Arbeitswurzel.")
        if name == "create":
            command.add_argument("--state", required=True, help="Ausdrücklich vorbereitete Inhaltsdatei.")
            command.add_argument("--creator", required=True)
            command.add_argument("--revision", type=int, default=1)
        elif name == "review":
            command.add_argument("--report", required=True, help="Separat erstellter Prüfbericht.")
            command.add_argument("--reviewer", required=True, help="Vom Aufrufer erwartete Prüferkennung.")
        elif name == "export":
            command.add_argument("--allow-unreviewed", action="store_true",
                                 help="Auch ungeprüften/veralteten Text mit sichtbarem Status ausgeben.")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    expected = {"expected_project_id": args.project, "expected_adapter_id": args.adapter}
    try:
        if args.command == "create":
            package = create_handoff(
                args.root, _json_input(args.root, args.state), args.source,
                project_id=args.project, adapter_id=args.adapter,
                creator_id=args.creator, revision=args.revision,
            )
            save_handoff(args.root, args.file, package)
            print("Übergabeentwurf gespeichert; unabhängiger Prüfbericht fehlt noch.")
            return 0
        # Capture raw bytes separately for compare-and-swap; a changed file is
        # rejected by save rather than overwritten with this operation's result.
        expected_file_sha256 = None
        if args.command == "review":
            expected_file_sha256 = hashlib.sha256(_read_input(args.root, args.file)).hexdigest()
        package = load_handoff(args.root, args.file)
        if args.command == "review":
            package = bind_review(
                args.root, package, _json_input(args.root, args.report), args.source,
                expected_reviewer_id=args.reviewer, **expected,
            )
            save_handoff(args.root, args.file, package,
                         expected_file_sha256=expected_file_sha256)
        if args.command == "export":
            print(render_handoff(args.root, package, args.source,
                                 require_review=not args.allow_unreviewed, **expected))
            return 0
        result = inspect_handoff(args.root, package, args.source, **expected)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "review_bound" else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print("Übergabe nicht freigegeben: " + str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
