#!/usr/bin/env python3
"""Run the Signalpost saved-data check with one command."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


EXPECTED = {
    "expected_changes": 2,
    "observed_changes": 2,
    "false_positive": 0,
    "evidence_complete": True,
    "idempotent_rerun": True,
}


def main() -> int:
    root = Path(__file__).resolve().parent
    runner = root / "scripts" / "run_refresh_replay.py"
    fixture = root / "tests" / "fixtures" / "refresh-snapshots.json"
    output = root / "out" / "refresh-demo.json"

    if not runner.is_file() or not fixture.is_file():
        print("Starter files are missing. Put first_run.py inside the extracted signalpost-starter-kit folder, then run it again.")
        return 2

    if sys.version_info < (3, 12):
        print(f"Python 3.12 or newer is required. This is Python {sys.version_info.major}.{sys.version_info.minor}.")
        return 2

    output.parent.mkdir(exist_ok=True)
    command = [
        sys.executable,
        str(runner),
        "--manifest", str(fixture),
        "--output", str(output),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("The saved-data check did not finish within 30 seconds. Send this exact blocker to submit@builderr.ai.")
        return 1

    if result.returncode:
        print("The saved-data check failed. Send the text below to submit@builderr.ai:")
        print(result.stdout.strip())
        return 1

    try:
        report = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"The check ran, but its result could not be read: {exc}")
        return 1

    wrong = {
        key: {"expected": expected, "actual": report.get(key)}
        for key, expected in EXPECTED.items()
        if report.get(key) != expected
    }
    if wrong:
        print("The saved-data result was not the expected result:")
        print(json.dumps(wrong, indent=2, sort_keys=True))
        return 1

    print("Signalpost starter: SUCCESS")
    print("It found both saved changes, added no false change, kept the evidence, and produced the same result on a repeat check.")
    print(f"Result: {output}")
    print("This is practice only. It does not enter or qualify your agent.")
    print("Next: open README.md and try the 10-company live run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
