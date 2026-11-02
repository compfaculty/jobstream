"""Reproducible deterministic matching baseline; never invokes a real provider."""

import json
import statistics
import time

from django.core.management.base import BaseCommand, CommandError

from jobs.models import ApplicationPreferences
from jobs.services.preference_matching import evaluate


class Command(BaseCommand):
    help = "Measure deterministic matching separately from agent execution."

    def add_arguments(self, parser):
        parser.add_argument("--iterations", type=int, default=1000)

    def handle(self, *args, **options):
        count = options["iterations"]
        if not 1 <= count <= 100000:
            raise CommandError("iterations must be between 1 and 100000")
        job = {
            "title": "Python engineer",
            "description": "Python backend and Linux",
            "location": "Tel Aviv",
        }
        policy = ApplicationPreferences()
        timings = []
        evaluate(job, policy, "Python and Linux")  # Warm regular expression caches.
        for _ in range(count):
            start = time.perf_counter()
            evaluate(job, policy, "Python and Linux")
            timings.append(time.perf_counter() - start)
        self.stdout.write(
            json.dumps(
                {
                    "iterations": count,
                    "fixture": "python-linux-v1",
                    "median_seconds": statistics.median(timings),
                    "max_seconds": max(timings),
                    "llm_tokens": 0,
                    "inference_cost": 0,
                    "note": "Pure Python matching; not equivalent to LLM advisory evaluation.",
                }
            )
        )
