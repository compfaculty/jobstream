import json
import resource
import statistics
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from jobs.models import Job
from jobs.services.search import search_jobs
from jobs.services.sources import RSSAdapter, normalize


class Command(BaseCommand):
    help = "Local search/parser benchmark; seeds only synthetic example.invalid jobs."

    def add_arguments(self, parser):
        parser.add_argument("--jobs", type=int, default=10000)
        parser.add_argument("--iterations", type=int, default=100)

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Benchmark data generation requires DEBUG=1")
        count = options["jobs"]
        iterations = options["iterations"]
        if not 100 <= count <= 100000 or not 10 <= iterations <= 1000:
            raise CommandError("Use 100–100000 jobs and 10–1000 iterations")
        Job.objects.bulk_create(
            [
                Job(
                    canonical_url=f"https://benchmark.example.invalid/jobs/{i}",
                    title=f"{'Python Backend' if i % 5 == 0 else 'Software'} Engineer {i}",
                    company=f"Example {i % 100}",
                    location="Tel Aviv" if i % 2 else "Remote",
                    description="Python PostgreSQL Linux backend engineering distributed systems "
                    * 10,
                )
                for i in range(count)
            ],
            ignore_conflicts=True,
            batch_size=500,
        )
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE jobs_job")
        durations = []
        query_counts = []
        for _ in range(iterations):
            start = time.perf_counter()
            with CaptureQueriesContext(connection) as queries:
                results = search_jobs("Python", "Tel Aviv")
                results.count()
                list(results[:25])
            durations.append((time.perf_counter() - start) * 1000)
            query_counts.append(len(queries))
        feed = (
            b'<rss version="2.0"><channel>'
            + b"".join(
                f"<item><title>Engineer {i}</title><link>https://example.com/{i}</link><description>Python</description></item>".encode()
                for i in range(100)
            )
            + b"</channel></rss>"
        )
        start = time.perf_counter()
        for _ in range(10):
            for listing in RSSAdapter().discover(feed, "https://example.com", 100):
                normalize(listing)
        parser_seconds = time.perf_counter() - start
        metrics = {
            "dataset_jobs": Job.objects.count(),
            "iterations": iterations,
            "search_ms_p50": round(statistics.median(durations), 2),
            "search_ms_p95": round(sorted(durations)[int(len(durations) * 0.95) - 1], 2),
            "queries_per_search": max(query_counts),
            "parser_listings_per_second": round(1000 / parser_seconds, 2),
            "process_max_rss_platform_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "error_count": 0,
        }
        self.stdout.write(json.dumps(metrics, indent=2))
