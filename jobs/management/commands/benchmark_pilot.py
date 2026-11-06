"""Reproducible real-posting extraction and synthetic comparison quality gate."""

import json
import re
import tempfile
import time
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from jobs.models import AICharge, AIResult
from jobs.services.agent_gateway import GatewayError
from jobs.services.ai import AIUnavailable, request_json
from jobs.services.evaluation_scoring import validate_assessment
from jobs.services.normalization import (
    needs_ai_normalization,
    normalize_description,
    normalize_with_ai,
    text_blocks,
)


def write_report(report: dict, destination: str | None) -> Path:
    if destination is not None:
        path = Path(destination)
        path.write_text(json.dumps(report, indent=2))
        return path
    # Atomic creation avoids shared-directory symlinks and restricts file permissions.
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="jobstream-pilot-quality-", suffix=".json", delete=False
    ) as stream:
        json.dump(report, stream, indent=2)
        return Path(stream.name)


class Command(BaseCommand):
    help = "Evaluate reviewed fixtures; --live permits bounded, shared-budget provider calls."

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true")
        parser.add_argument(
            "--report", help="Report path; defaults to a unique private temporary file"
        )
        parser.add_argument(
            "--fixtures", default=str(settings.BASE_DIR / "tests/fixtures/ai_pilot")
        )

    def handle(self, *args, **options):
        root = Path(options["fixtures"])
        rows, correct, checks, unsupported = [], 0, 0, 0
        charges_before = AICharge.objects.count()
        prompt_file = settings.BASE_DIR / "services/infinity-agent/src/assessment.rs"
        prompt = json.loads(
            re.search(r'pub const PROMPT: &str = ("(?:[^"\\]|\\.)*");', prompt_file.read_text())[1]
        )
        prompt += " Authorized context is supplied in input; do not call tools."
        fields = ("requirement_id", "status", "evidence_fact_id", "evidence_quote", "explanation")
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "assessments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {f: {"type": "string"} for f in fields},
                        "required": list(fields),
                    },
                },
            },
            "required": ["summary", "confidence", "assessments"],
        }
        public = json.loads((root / "public_jobs.json").read_text())
        for case in public:
            start = time.monotonic()
            normalized = normalize_description(case["description"])
            row = {
                "kind": "normalization",
                "id": case["id"],
                "reviewed": case["reviewed"],
                "label_checks": len(case["labels"]) + bool(case.get("expected_empty")),
                "label_correct": 0,
            }
            if case["reviewed"]:
                checks += len(case["labels"]) + bool(case.get("expected_empty"))
            try:
                if (
                    options["live"]
                    and case["reviewed"]
                    and not case.get("expected_empty")
                    and needs_ai_normalization(normalized)
                ):
                    normalized = normalize_with_ai(case["description"], normalized)
                lines = text_blocks(case["description"]).splitlines()
                # Candidate labels are reported separately and never pass a reviewed quality gate.
                for label in case["labels"]:
                    matched = lines[label["line"]] in normalized.get(label["field"], [])
                    if case["reviewed"]:
                        correct += matched
                        row["label_correct"] += matched
                if case.get("expected_empty"):
                    empty_match = all(
                        not normalized.get(f)
                        for f in ("summary", "requirements", "preferred", "responsibilities")
                    )
                    correct += empty_match
                    row["label_correct"] += empty_match
                row["state"] = "reviewed" if case["reviewed"] else "needs_label_review"
            except AIUnavailable as exc:
                row["state"] = exc.code
            row["seconds"] = round(time.monotonic() - start, 3)
            rows.append(row)
        comparisons = json.loads((root / "comparisons.json").read_text())
        comparison_correct = comparison_checks = 0
        for case in comparisons:
            row = {"kind": "comparison", "id": case["id"], "state": "not_run"}
            if options["live"]:
                snapshot = {
                    "criteria": [
                        {"id": f"r{i}", "text": t, "weight": 3}
                        for i, t in enumerate(case["requirements"])
                    ],
                    "facts": [{"id": "f1", "text": case["cv"]}],
                    "preference_blocks": [],
                    "fit": {"state": "eligible"},
                }
                try:
                    start = time.monotonic()
                    output = request_json(
                        None,
                        "pilot_comparison",
                        snapshot,
                        schema,
                        prompt,
                        model=settings.INFINITY_MODEL,
                        reasoning_effort=settings.INFINITY_REASONING_EFFORT,
                        max_output_tokens=800,
                        timeout_seconds=20,
                    )
                    validate_assessment(snapshot, output)
                    statuses = {a["requirement_id"]: a["status"] for a in output["assessments"]}
                    comparison_checks += len(case["expected"])
                    comparison_correct += sum(
                        statuses.get(f"r{i}") == v for i, v in enumerate(case["expected"])
                    )
                    row.update(state="completed", seconds=round(time.monotonic() - start, 3))
                except (AIUnavailable, GatewayError) as exc:
                    row["state"] = exc.code
                    unsupported += exc.code == "agent_unsupported_claim"
            rows.append(row)
        from django.db.models import Sum

        usage = AICharge.objects.order_by("pk")[charges_before:].aggregate(
            reserved=Sum("reserved_usd"), charged=Sum("charged_usd")
        )
        reviewed = sum(
            bool(c["reviewed"] and (c["labels"] or c.get("expected_empty"))) for c in public
        )
        passed = (
            reviewed >= 100
            and checks > 0
            and correct / checks >= 0.95
            and comparison_checks >= 30
            and comparison_correct / comparison_checks >= 0.95
            and unsupported == 0
        )
        measured = sum(r.get("label_checks", 0) for r in rows if r["state"] == "reviewed")
        report = {
            "measured_normalization_accuracy": correct / measured if measured else None,
            "comparison_accuracy": comparison_correct / comparison_checks
            if comparison_checks
            else None,
            "budget_blocked": sum(
                r["state"] in {"ai_daily_budget_exceeded", "ai_monthly_budget_exceeded"}
                for r in rows
            ),
            "states": dict(Counter(r["state"] for r in rows)),
            "passed": passed,
            "live": options["live"],
            "public_postings": len(public),
            "reviewed_postings": reviewed,
            "normalization_correct": correct,
            "normalization_checks": checks,
            "comparison_correct": comparison_correct,
            "comparison_checks": comparison_checks,
            "unsupported": unsupported,
            "usage_estimates": {k: str(v) for k, v in usage.items()},
            "cache_reuses": AIResult.objects.aggregate(n=Sum("cache_hits"))["n"] or 0,
            "rows": rows,
        }
        path = write_report(report, options["report"])
        summary = {k: v for k, v in report.items() if k != "rows"}
        summary["report_path"] = str(path)
        self.stdout.write(json.dumps(summary))
