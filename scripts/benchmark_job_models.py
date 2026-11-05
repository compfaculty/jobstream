"""Bounded live smoke comparison on synthetic fixtures, never user CVs.

Run as `python -m scripts.benchmark_job_models --live`. Prices are uncached Standard
rates verified 2026-11-05; this reports conservative estimates, not invoiced cost.
"""

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path

VARIANTS = (
    ("gpt-4.1-mini", None, 0.4, 1.6),
    ("gpt-6-luna", "none", 0.1, 0.5),
    ("gpt-6-luna", "low", 0.1, 0.5),
)


def normalization_fixtures():
    return [
        (
            "headings",
            [
                "We are a world-leading innovative company.",
                "Our platform detects network threats.",
                "Responsibilities",
                "Build Go services.",
                "Requirements",
                "Three years of Go or Rust experience.",
                "Advantages",
                "Production CUDA experience.",
                "Benefits",
                "Free lunch.",
            ],
            {
                "summary": [3],
                "product": [1],
                "requirements": [5],
                "preferred": [7],
                "responsibilities": [3],
            },
        ),
        (
            "plain_prose",
            [
                "Join our inspiring mission!",
                "You will build Python APIs.",
                "You must have three years of Python experience.",
                "Kubernetes is a bonus.",
                "Our product manages cloud storage.",
            ],
            {
                "summary": [1],
                "product": [4],
                "requirements": [2],
                "preferred": [3],
                "responsibilities": [1],
            },
        ),
    ]


def comparison_fixtures():
    return [
        (
            "alternatives",
            "Five years building Go backend services; four years leading a team.",
            [
                "At least three years of Go OR C++ OR Java.",
                "Three years of team leadership.",
                "Experience operating GPU inference services.",
            ],
            ["met", "met", "not_demonstrated"],
        ),
        (
            "years",
            "Two years leading an engineering team. Four years developing Go services.",
            [
                "At least three years leading a team.",
                "Three years of Go development.",
                "Production CUDA deployment experience.",
            ],
            ["partial", "met", "not_demonstrated"],
        ),
        (
            "negation",
            "Five years writing Python APIs. Never worked with GPUs or ML inference.",
            ["Three years of production ML inference.", "CUDA deployment experience."],
            ["not_demonstrated", "not_demonstrated"],
        ),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-cost-usd", type=float, default=0.05)
    args = parser.parse_args()
    if not args.live:
        print("Use --live to run synthetic paid API smoke checks (default estimated cap: $0.05).")
        return
    if not 0 < args.max_cost_usd <= 0.10:
        raise SystemExit("Live smoke cap must be positive and at most $0.10.")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from jobs.services.agent_gateway import GatewayError
    from jobs.services.ai import AIUnavailable, request_json
    from jobs.services.evaluation_scoring import validate_assessment
    from jobs.services.normalization import NORMALIZATION_PROMPT, section_schema

    rust = (
        Path(__file__).resolve().parents[1] / "services/infinity-agent/src/assessment.rs"
    ).read_text()
    prompt = json.loads(re.search(r'pub const PROMPT: &str = ("(?:[^"\\]|\\.)*");', rust)[1])
    prompt += (
        " For this smoke test the authorized context is already supplied in input; "
        "do not call tools."
    )
    fields = ("requirement_id", "status", "evidence_fact_id", "evidence_quote", "explanation")
    cv_schema = {
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
                    "properties": {field: {"type": "string"} for field in fields},
                    "required": list(fields),
                },
            },
        },
        "required": ["summary", "confidence", "assessments"],
    }
    cases = []
    for name, lines, expected in normalization_fixtures():
        cases.append(
            (
                "normalization",
                name,
                {"lines": list(enumerate(lines))},
                section_schema(),
                NORMALIZATION_PROMPT,
                800,
                expected,
                None,
            )
        )
    for name, text, requirements, statuses in comparison_fixtures():
        snapshot = {
            "assessment_version": 2,
            "facts": [{"id": "f1", "text": text}],
            "criteria": [
                {"id": f"req-{i + 1}", "text": req, "weight": 3}
                for i, req in enumerate(requirements)
            ],
            "preference_blocks": [],
            "fit": {"state": "eligible"},
        }
        cases.append(
            ("comparison", name, {"context": snapshot}, cv_schema, prompt, 1500, statuses, snapshot)
        )
    rows, spent = [], 0.0
    events = []

    class Capture(logging.Handler):
        def emit(self, record):
            if record.msg == "ai_request_completed":
                events.append(record)

    capture = Capture()
    logging.getLogger("jobs.services.ai").addHandler(capture)
    try:
        for model, effort, input_price, output_price in VARIANTS:
            for task, name, payload, schema, instructions, limit, expected, snapshot in cases:
                bound = (
                    (len(json.dumps(payload).encode()) + len(instructions.encode()) + 2048)
                    * input_price
                    + limit * output_price
                ) / 1_000_000
                if spent + bound > args.max_cost_usd:
                    raise SystemExit("Smoke cost cap reached; no further calls made.")
                events.clear()
                started = time.monotonic()
                row = {"model": model, "reasoning": effort, "task": task, "fixture": name}
                try:
                    output = request_json(
                        None,
                        "benchmark_job_" + task,
                        payload,
                        schema,
                        instructions,
                        model=model,
                        reasoning_effort=effort,
                        max_output_tokens=limit,
                    )
                    if snapshot:
                        validate_assessment(snapshot, output)
                        statuses = {
                            item["requirement_id"]: item["status"] for item in output["assessments"]
                        }
                        row["correct"] = sum(
                            statuses.get(f"req-{i + 1}") == value
                            for i, value in enumerate(expected)
                        )
                        row["checks"] = len(expected)
                    else:
                        source_lines = payload["lines"]
                        if set(output) != set(expected):
                            raise ValueError("Invalid normalization fields")
                        for field, indices in output.items():
                            if (
                                not isinstance(indices, list)
                                or len(indices) > (2 if field in {"summary", "product"} else 40)
                                or any(
                                    type(i) is not int or not 0 <= i < len(source_lines)
                                    for i in indices
                                )
                                or len(set(indices)) != len(indices)
                            ):
                                raise ValueError("Invalid source indices")
                        classified = sum(
                            (
                                output[field]
                                for field in ("requirements", "preferred", "responsibilities")
                            ),
                            [],
                        )
                        if len(classified) != len(set(classified)):
                            raise ValueError("Conflicting categories")
                        row["correct"] = sum(
                            output.get(key) == value for key, value in expected.items()
                        )
                        row["checks"] = len(expected)
                    row["valid"] = True
                except (AIUnavailable, GatewayError, ValueError, KeyError) as exc:
                    row.update(
                        valid=False,
                        error=getattr(exc, "code", type(exc).__name__),
                        correct=0,
                        checks=len(expected),
                    )
                if events:
                    event = events[-1]
                    cost = (
                        (event.input_tokens or 0) * input_price
                        + (event.output_tokens or 0) * output_price
                    ) / 1_000_000
                    row.update(
                        input_tokens=event.input_tokens,
                        output_tokens=event.output_tokens,
                        cached_result=False,
                    )
                else:
                    cost = 0 if row["valid"] else bound
                    row["cached_result"] = row["valid"]
                spent += cost
                row.update(
                    seconds=round(time.monotonic() - started, 3), estimated_usd=round(cost, 6)
                )
                rows.append(row)
                print("SMOKE_PROGRESS=" + json.dumps(row), flush=True)
    finally:
        logging.getLogger("jobs.services.ai").removeHandler(capture)
    print(
        "SMOKE_RESULTS="
        + json.dumps({"synthetic_only": True, "estimated_usd": round(spent, 6), "rows": rows}),
        flush=True,
    )


if __name__ == "__main__":
    main()
