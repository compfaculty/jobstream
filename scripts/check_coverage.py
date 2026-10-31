"""Require branch and statement coverage separately for the trust-boundary services."""

import json
from pathlib import Path

report = json.loads(Path("coverage.json").read_text())
for name in (
    "jobs/services/authorization.py",
    "jobs/services/submissions.py",
    "jobs/services/gmail.py",
):
    summary = report["files"][name]["summary"]
    for total, covered in (
        ("num_statements", "covered_lines"),
        ("num_branches", "covered_branches"),
    ):
        percent = 100 * summary[covered] / summary[total] if summary[total] else 100
        if percent < 90:
            raise SystemExit(f"{name}: {covered} {percent:.1f}% < 90%")
print("Authorization, submission, Gmail: statement and branch coverage >= 90%")
