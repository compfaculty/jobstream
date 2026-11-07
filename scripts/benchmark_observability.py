"""Synthetic /health/ WSGI baseline. Run in separate processes for each source tree."""

import argparse
import json
import logging
import os
import resource
import statistics
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--requests", type=int, default=10000)
    args = parser.parse_args()
    sys.path.insert(0, args.source)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DEBUG", "1")
    import django

    django.setup()
    # Keep stdout available for the result and prevent console I/O from dominating.
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(open(os.devnull, "w"))
    from django.test import Client

    client = Client(HTTP_HOST="localhost")
    for _ in range(200):
        assert client.get("/health/").status_code == 200
    samples = []
    start_cpu = time.process_time()
    start = time.perf_counter()
    for _ in range(args.requests):
        before = time.perf_counter()
        assert client.get("/health/").status_code == 200
        samples.append(time.perf_counter() - before)
    elapsed = time.perf_counter() - start
    print(
        json.dumps(
            {
                "requests": args.requests,
                "requests_per_second": round(args.requests / elapsed, 1),
                "p95_ms": round(statistics.quantiles(samples, n=100)[94] * 1000, 3),
                "cpu_seconds": round(time.process_time() - start_cpu, 3),
                "max_rss_native": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "platform": sys.platform,
            }
        )
    )


if __name__ == "__main__":
    main()
