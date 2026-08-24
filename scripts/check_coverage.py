#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


# These floors are deliberately per-module: aggregate coverage alone can hide an
# untested trust boundary behind well-covered data/model code. Raise them as the
# corresponding failure-injection suites expand; never lower them to make CI green.
FLOORS: dict[str, float] = {
    "src/omarchy_yolo/orchestrator.py": 72.0,
    "src/omarchy_yolo/orchestrator_integration.py": 60.0,
    "src/omarchy_yolo/orchestrator_task.py": 65.0,
    "src/omarchy_yolo/git.py": 83.0,
    "src/omarchy_yolo/gates.py": 90.0,
    "src/omarchy_yolo/process.py": 90.0,
    "src/omarchy_yolo/db.py": 80.0,
    "src/omarchy_yolo/db_core.py": 90.0,
    "src/omarchy_yolo/db_records.py": 82.0,
    "src/omarchy_yolo/db_recovery.py": 80.0,
    "src/omarchy_yolo/db_provenance.py": 80.0,
    "src/omarchy_yolo/state_machine.py": 95.0,
    "src/omarchy_yolo/resources.py": 85.0,
    "src/omarchy_yolo/dossier_cli.py": 75.0,
    "src/omarchy_yolo/runtime.py": 94.0,
    "src/omarchy_yolo/sandbox.py": 95.0,
    "src/omarchy_yolo/review_source.py": 85.0,
    "src/omarchy_yolo/reviewer.py": 75.0,
}


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = payload.get("files")
    if not isinstance(files, dict):
        raise SystemExit("coverage JSON does not contain a files object")

    failures: list[str] = []
    for filename, floor in FLOORS.items():
        entry = files.get(filename)
        if not isinstance(entry, dict):
            failures.append(f"{filename}: missing from coverage report")
            continue
        summary = entry.get("summary")
        if not isinstance(summary, dict):
            failures.append(f"{filename}: missing coverage summary")
            continue
        percent_raw = summary.get("percent_covered")
        if not isinstance(percent_raw, (int, float)):
            failures.append(f"{filename}: invalid percent_covered")
            continue
        percent = float(percent_raw)
        print(f"coverage floor {filename}: {percent:.2f}% >= {floor:.2f}%")
        if percent + 1e-9 < floor:
            failures.append(f"{filename}: {percent:.2f}% < required {floor:.2f}%")

    if failures:
        print("critical-module coverage gate failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
