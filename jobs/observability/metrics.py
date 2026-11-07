"""Bounded operational metrics. No object identifiers or remote text in labels."""

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar

from prometheus_client import Counter, Gauge, Histogram

BUCKETS = (0.005, 0.025, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 30, 60, 180)
QUEUES = frozenset({"celery", "ingestion", "preparation", "delivery", "browser-delivery"})
provider_context = ContextVar("metric_provider", default="other")
HTTP = Counter(
    "jobstream_http_requests_total", "Completed HTTP requests", ["route", "method", "status"]
)
LATENCY = Histogram(
    "jobstream_http_duration_seconds",
    "HTTP response lifetime",
    ["route", "method"],
    buckets=BUCKETS,
)
INFLIGHT = Gauge("jobstream_http_in_progress", "Active HTTP responses", multiprocess_mode="livesum")
PAYLOAD = Counter("jobstream_http_payload_bytes_total", "Observed response payload bytes")
UNKNOWN = Counter(
    "jobstream_http_payload_unknown_total", "Responses without observable payload size"
)
OUTBOUND = Counter(
    "jobstream_outbound_attempts_total", "HTTP attempts", ["provider", "status_class", "error"]
)
OUTBOUND_TIME = Histogram(
    "jobstream_outbound_duration_seconds", "HTTP attempt latency", ["provider"], buckets=BUCKETS
)
OUTBOUND_BYTES = Counter(
    "jobstream_outbound_payload_bytes_total",
    "Observed decoded body bytes, not wire traffic",
    ["provider"],
)
OUTBOUND_UNKNOWN = Counter(
    "jobstream_outbound_payload_unknown_total", "Unobserved response body sizes", ["provider"]
)
THROTTLED = Counter("jobstream_outbound_throttles_total", "HTTP 429 responses", ["provider"])
TASKS = Counter("jobstream_tasks_total", "Task lifecycle transitions", ["task", "queue", "state"])
TASK_TIME = Histogram(
    "jobstream_task_duration_seconds", "Task attempt duration", ["task", "queue"], buckets=BUCKETS
)
RUNNING = Gauge(
    "jobstream_tasks_running",
    "Running task attempts",
    ["task", "queue"],
    multiprocess_mode="livesum",
)
INGESTION = Counter("jobstream_ingestion_runs_total", "Finalized ingestion runs", ["outcome"])
NEW_JOBS = Counter("jobstream_ingestion_new_jobs_total", "New Job rows from finalized runs")
OBSERVATIONS = Counter(
    "jobstream_ingestion_observations_total", "Accepted new or existing job observations"
)
LOG_DROPS = Counter("jobstream_log_dropped_total", "File events dropped", ["reason"])
LOG_BUFFER = Gauge(
    "jobstream_log_buffer_events", "Queued asynchronous file events", multiprocess_mode="livesum"
)
PROVIDERS = frozenset(
    {
        "brave",
        "pituchim",
        "arbeitnow",
        "himalayas",
        "greenhouse",
        "ashby",
        "workday",
        "comeet",
        "infinity",
        "ai",
        "robots",
        "rss",
        "secret_telaviv",
        "telfed",
        "jsonld",
        "realwork",
        "40seas",
        "agrematch",
        "ai21",
        "akeyless",
        "audiocodes",
        "other",
    }
)


def enabled():
    return os.getenv("JOBSTREAM_METRICS_ENABLED", "0") == "1"


def observe(callback, *args):
    """Fail open only for telemetry; never wrap the business operation itself."""
    if enabled():
        try:
            callback(*args)
        except OSError, ValueError:
            pass


def provider(value):
    return value if value in PROVIDERS else "other"


def remote_provider(url):
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.path == "/robots.txt":
        return "robots"
    for suffix, name in (
        ("api.search.brave.com", "brave"),
        ("pituchim.co.il", "pituchim"),
        ("arbeitnow.com", "arbeitnow"),
        ("himalayas.app", "himalayas"),
        ("greenhouse.io", "greenhouse"),
        ("ashbyhq.com", "ashby"),
        ("myworkdayjobs.com", "workday"),
        ("comeet.co", "comeet"),
    ):
        if host == suffix or host.endswith("." + suffix):
            return name
    return provider_context.get()


@contextmanager
def integration(value):
    token = provider_context.set(provider(value))
    try:
        yield
    finally:
        provider_context.reset(token)


class Attempt:
    def __init__(self, name):
        self.name = provider(name)
        self.status = 0
        self.size = None
        self.started = time.monotonic()

    def finish(self, exc=None):
        code = "none"
        if exc is not None:
            code = "timeout" if "timeout" in type(exc).__name__.lower() else "failure"
        status = f"{self.status // 100}xx" if 100 <= self.status <= 599 else "unknown"
        observe(lambda: OUTBOUND.labels(self.name, status, code).inc())
        observe(lambda: OUTBOUND_TIME.labels(self.name).observe(time.monotonic() - self.started))
        if self.size is None:
            observe(lambda: OUTBOUND_UNKNOWN.labels(self.name).inc())
        else:
            observe(lambda: OUTBOUND_BYTES.labels(self.name).inc(self.size))
        if self.status == 429:
            observe(lambda: THROTTLED.labels(self.name).inc())


@contextmanager
def outbound(name=None):
    attempt = Attempt(name or provider_context.get())
    try:
        yield attempt
    except BaseException as exc:
        attempt.finish(exc)
        raise
    else:
        attempt.finish()
