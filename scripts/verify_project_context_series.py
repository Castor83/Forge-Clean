"""Three fixed fresh dialogue runs; retain failures and never retry within a series."""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import verify_project_context_dialogues as dialogues
from scripts import verify_project_context_live as live


def main(argv=None):
    parser = argparse.ArgumentParser(description="Drei unveränderte vollständige Dialogläufe abnehmen.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--model", default="forge-30b")
    args = parser.parse_args(argv)
    directory = live.selected_workspace(args.workspace, prepare=True)
    initial = dialogues.source_hashes()
    own_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report = {"model": args.model, "fixed_run_count": 3, "passed": False,
              "source_sha256": initial, "series_script_sha256": own_hash, "runs": []}
    started = time.monotonic()
    try:
        for number in range(1, 4):
            child = directory / ("run-" + str(number))
            exit_code = dialogues.main(["--workspace", str(child), "--model", args.model])
            actual = json.loads((child / "acceptance_project_dialogues_report.json").read_text(encoding="utf-8"))
            report["runs"].append({"number": number, "exit_code": exit_code, "workspace": str(child),
                                   "result": actual})
            if (dialogues.source_hashes() != initial
                    or hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != own_hash):
                raise ValueError("Quellen während Serie geändert; Serie nicht gültig.")
        report["passed"] = all(item["exit_code"] == 0 and item["result"]["passed"]
                               for item in report["runs"])
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["source_files_unchanged"] = (dialogues.source_hashes() == initial
            and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == own_hash)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["passed"] = report["passed"] and report["source_files_unchanged"]
        dialogues.save_report(directory, "acceptance_project_dialogue_series_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
