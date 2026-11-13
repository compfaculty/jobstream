"""Observable contracts, privacy boundaries, and real multiprocess lifecycle checks."""

import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory
from prometheus_client import generate_latest

from jobs.observability import metrics
from jobs.observability.context import context, identifier, new_context
from jobs.observability.exporter import SnapshotCollector
from jobs.observability.http import stream
from jobs.observability.logging import AsyncFileHandler, SafeFormatter
from jobs.observability.middleware import TelemetryMiddleware

SECRET = "fake-password-and-private-applicant@example.com"


def record(name="jobs.test", message="task_finished", **fields):
    return logging.makeLogRecord(
        {"name": name, "msg": message, "levelname": "INFO", "levelno": 20, **fields}
    )


def test_safe_logs_drop_secrets_and_exception_messages():
    try:
        raise ValueError(SECRET)
    except ValueError:
        info = sys.exc_info()
    line = SafeFormatter().format(
        record(
            message="failure\n" + SECRET,
            args=(SECRET,),
            password=SECRET,
            exc_info=info,
            task_args=[SECRET],
            url=SECRET,
        )
    )
    assert SECRET not in line
    value = json.loads(line)
    assert value["event"] == "runtime_event"
    assert value["error_category"] == "exception"
    assert value["frames"]
    assert "\n" not in line
    third_party = SafeFormatter().format(record("httpx", SECRET, url=SECRET))
    assert SECRET not in third_party


@pytest.mark.parametrize(
    "value", ["", "x" * 1000, "a\nAuthorization: secret", SECRET, "0" * 36, None]
)
def test_untrusted_correlation_rejected(value):
    assert identifier(value) is None
    assert identifier(new_context({"workflow_id": value})["workflow_id"])


def test_concurrent_context_isolation():
    def execute(_):
        value = str(uuid4())
        token = context.set({"workflow_id": value})
        try:
            time.sleep(0.001)
            return json.loads(SafeFormatter().format(record()))["workflow_id"] == value
        finally:
            context.reset(token)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(execute, range(100)))
    assert not context.get()


def test_http_metrics_routes_and_stream_close(monkeypatch, caplog, db):
    monkeypatch.setenv("JOBSTREAM_METRICS_ENABLED", "1")
    request = RequestFactory().get("/unknown/" + SECRET, HTTP_X_REQUEST_ID=SECRET)
    response = TelemetryMiddleware(lambda request: HttpResponse(b"ok"))(request)
    assert identifier(response["X-Request-ID"])
    assert not context.get()
    assert metrics.HTTP.labels("unknown", "GET", "200")._value.get() >= 1
    assert SECRET not in generate_latest().decode()
    assert SECRET not in caplog.text
    response = TelemetryMiddleware(lambda request: StreamingHttpResponse(iter([b"a", b"b"])))(
        request
    )
    before = metrics.INFLIGHT._value.get()
    assert before >= 1
    response.close()
    response.close()
    assert metrics.INFLIGHT._value.get() == before - 1


def test_unhandled_exception_decrements_gauge(monkeypatch):
    monkeypatch.setenv("JOBSTREAM_METRICS_ENABLED", "1")
    before = metrics.INFLIGHT._value.get()

    def failure(request):
        raise RuntimeError(SECRET)

    with pytest.raises(RuntimeError):
        TelemetryMiddleware(failure)(RequestFactory().get("/"))
    assert metrics.INFLIGHT._value.get() == before
    assert not context.get()


def test_httpx_boundaries_keep_body_private(monkeypatch):
    monkeypatch.setenv("JOBSTREAM_METRICS_ENABLED", "1")
    workflow = str(uuid4())
    token = context.set({"workflow_id": workflow})
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(429, content=SECRET.encode())

    try:
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            with stream(client, "POST", "http://test.invalid", provider="infinity") as response:
                assert response.read() == SECRET.encode()
        assert seen[0].headers["X-Jobstream-workflow-id"] == workflow
        assert metrics.THROTTLED.labels("infinity")._value.get() >= 1
        assert SECRET not in generate_latest().decode()
    finally:
        context.reset(token)


def test_publish_retry_and_task_context_restore():
    from jobs.task_telemetry import (
        finished,
        metric_finished,
        metric_started,
        publish_context,
        started,
    )

    headers = {}
    publish_context(headers=headers)
    original = headers["workflow_id"]
    publish_context(headers=headers)
    assert headers["workflow_id"] == original
    task = SimpleNamespace(
        name="jobs.tasks.collect_operations",
        request=SimpleNamespace(headers=headers, delivery_info={"routing_key": "celery"}),
    )
    started(sender=task, task_id=str(uuid4()))
    metric_started(sender=task)
    assert context.get()["workflow_id"] == original
    finished(sender=task, state="RETRY")
    metric_finished(sender=task, state="RETRY")
    assert not context.get()


@pytest.mark.django_db(transaction=True)
def test_snapshot_exporter_is_bounded_read_only(django_assert_num_queries):
    from django.utils import timezone

    from jobs.models import Company, OperationalSnapshot, Source

    OperationalSnapshot.objects.create(
        key="infrastructure",
        observed_at=timezone.now(),
        payload={"queues": [{"name": "ingestion", "depth": 3}], "workers": []},
    )
    Company.objects.create(name="Private company", review_required=True)
    Source.objects.create(
        name="Private source", adapter="rss", url="https://example.com/feed", discovery_pending=True
    )
    # PostgreSQL transaction/savepoint setup plus three read queries; no probes.
    collector = SnapshotCollector()
    values = list(collector.collect())
    assert (
        next(v.samples[0].value for v in values if v.name == "jobstream_pending_company_reviews")
        == 1
    )
    with django_assert_num_queries(0):
        list(collector.collect())
    assert "Private" not in str(values)


def test_file_handler_rotation_and_outage(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSTREAM_LOG_DIR", str(tmp_path))
    handler = AsyncFileHandler()
    handler.setFormatter(SafeFormatter())
    handler.emit(record(password=SECRET))
    handler.flush()
    files = list(tmp_path.glob("events-*.jsonl"))
    assert len(files) == 1
    assert SECRET not in files[0].read_text()
    handler.writer.maxBytes = 200
    for _ in range(20):
        handler.emit(record())
    handler.flush()
    assert len(list(tmp_path.glob("events-*"))) <= 3
    # Failed file delivery cannot raise into the caller.
    monkeypatch.setattr(
        handler.writer, "emit", lambda record: (_ for _ in ()).throw(OSError(SECRET))
    )
    handler.emit(record())
    handler.flush()


def test_real_multiprocess_counts_and_dead_gauges(tmp_path):
    env = {
        **os.environ,
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path),
        "JOBSTREAM_METRICS_ENABLED": "1",
    }
    code = (
        "from jobs.observability.metrics import HTTP, INFLIGHT; "
        "HTTP.labels('known','GET','200').inc(); INFLIGHT.inc()"
    )
    processes = [subprocess.Popen([sys.executable, "-c", code], env=env) for _ in range(2)]
    for process in processes:
        assert process.wait(timeout=15) == 0
    # Aggregate in a fresh process: the client chooses multiprocess mode at import.
    scrape = (
        "from prometheus_client import CollectorRegistry,generate_latest,multiprocess; "
        "r=CollectorRegistry(); multiprocess.MultiProcessCollector(r); "
        "print(generate_latest(r).decode())"
    )
    data = subprocess.check_output([sys.executable, "-c", scrape], env=env, text=True)
    assert 'jobstream_http_requests_total{method="GET",route="known",status="200"} 2.0' in data
    assert "jobstream_http_in_progress 2.0" in data
    cleanup = "from prometheus_client import multiprocess; " + "; ".join(
        f"multiprocess.mark_process_dead({p.pid})" for p in processes
    )
    subprocess.run([sys.executable, "-c", cleanup], env=env, check=True)
    data = subprocess.check_output([sys.executable, "-c", scrape], env=env, text=True)
    assert "jobstream_http_in_progress 2.0" not in data
    assert 'status="200"} 2.0' in data


def test_valid_looking_remote_error_is_not_a_log_field():
    secret = "private_token_123456"
    line = SafeFormatter().format(record(message=secret, error_code=secret, model=secret))
    assert secret not in line
    assert json.loads(line)["model"] == "configured"


def test_exporter_failure_does_not_terminate_business_process(tmp_path):
    token = tmp_path / "invalid-token"
    token.write_text("short")
    env = {
        **os.environ,
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path / "metrics"),
        "JOBSTREAM_METRICS_ENABLED": "1",
        "JOBSTREAM_SCRAPE_TOKEN_FILE": str(token),
    }
    process = subprocess.run(
        [
            sys.executable,
            "scripts/telemetry_entrypoint.py",
            sys.executable,
            "-c",
            'import time;time.sleep(1.5);print("business-completed")',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0
    assert "business-completed" in process.stdout


@pytest.mark.parametrize(
    "origin",
    [
        "javascript:alert(1)",
        "https://user:password@example.com",
        "http://[broken",
        "http://localhost:invalid",
        "https://example.com/path?secret=private",
    ],
)
def test_invalid_grafana_origin_never_breaks_operations(settings, origin):
    from jobs.operations_views import grafana_link

    settings.GRAFANA_URL = origin
    assert grafana_link() == ""


def test_grafana_correlation_link_is_exact_and_escaped(settings):
    from urllib.parse import parse_qs, urlsplit

    from jobs.operations_views import grafana_link

    settings.GRAFANA_URL = "http://localhost:3000/"
    value = str(uuid4())
    link = urlsplit(grafana_link(value))
    assert link.netloc == "localhost:3000"
    assert link.path == "/d/jobstream-investigations"
    assert parse_qs(link.query)["var-correlation"] == [value]


def test_monitoring_setup_keeps_credentials_private_and_is_idempotent(
    tmp_path, monkeypatch, capsys
):
    from scripts import setup_monitoring

    monkeypatch.setattr(setup_monitoring, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["setup_monitoring", "--infinity"])
    calls = []
    monkeypatch.setattr(
        setup_monitoring.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    setup_monitoring.main()
    directory = tmp_path / ".monitoring-secrets"
    first = (directory / "grafana-admin").read_text()
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / "grafana-admin").stat().st_mode & 0o777) == 0o600
    assert first.strip() not in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["setup_monitoring"])
    setup_monitoring.main()
    assert (directory / "grafana-admin").read_text() == first
    assert "infinity:9100" in (directory / "infinity-targets.json").read_text()
    assert all("credential" not in " ".join(command) for command, _ in calls)


def test_internal_exporter_authentication_and_non_ascii_headers(tmp_path):
    import socket

    from scripts.observability_lifecycle import until

    token = tmp_path / "scrape-token"
    token.write_text("fake-scrape-credential-only-for-this-test-0123456789")
    directory = tmp_path / "metrics"
    directory.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PROMETHEUS_MULTIPROC_DIR": str(directory),
        "JOBSTREAM_SCRAPE_TOKEN_FILE": str(token),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", f"from jobs.observability.exporter import serve; serve({port})"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        until(lambda: httpx.get(url + "/metrics", timeout=0.3).status_code == 401)
        response = httpx.get(
            url + "/metrics", headers={"Authorization": "Bearer " + token.read_text()}
        )
        assert response.status_code == 200
        assert token.read_text() not in response.text
        assert httpx.get(url + "/metrics?private=hidden").status_code == 404
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"GET /metrics HTTP/1.0\r\nAuthorization: Bearer \xff\r\n\r\n")
            assert b"401" in sock.recv(1024)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_failed_stdout_never_uses_logging_raw_fallback(capsys):
    from jobs.observability.logging import SafeStreamHandler

    class BrokenStream:
        def write(self, text):
            raise OSError("synthetic disk pressure")

        def flush(self):
            pass

    handler = SafeStreamHandler(BrokenStream())
    handler.setFormatter(SafeFormatter())
    handler.emit(record("httpx", SECRET, args=(SECRET,)))
    captured = capsys.readouterr()
    assert SECRET not in captured.err + captured.out


@pytest.mark.django_db
def test_new_job_metrics_exclude_repeat_observations_and_rollback(monkeypatch):
    from unittest.mock import Mock

    from django.utils import timezone

    from jobs.models import Job, Source
    from jobs.services import ingestion
    from jobs.services.sources import Listing

    monkeypatch.setenv("JOBSTREAM_METRICS_ENABLED", "1")
    before = metrics.NEW_JOBS._value.get()
    observations = metrics.OBSERVATIONS._value.get()
    source = Source.objects.create(
        name="Synthetic", adapter="rss", url="https://example.com/feed", enabled=True
    )
    adapter = Mock(
        discover=Mock(return_value=[Listing("https://example.com/synthetic-job", "Engineer")])
    )
    monkeypatch.setitem(ingestion.ADAPTERS, "rss", adapter)
    monkeypatch.setattr(ingestion, "fetch_public", lambda url: b"fake-feed")
    for _ in range(2):
        Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
        ingestion.ingest(source.pk)
    assert Job.objects.count() == 1
    assert metrics.NEW_JOBS._value.get() - before == 1
    assert metrics.OBSERVATIONS._value.get() - observations == 2
    adapter.discover.return_value = [Listing("https://example.com/rolled-back-job", "Engineer")]
    monkeypatch.setattr(ingestion, "from_listing", Mock(side_effect=OSError("private failure")))
    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
    ingestion.ingest(source.pk)
    assert Job.objects.count() == 1
    assert metrics.NEW_JOBS._value.get() - before == 1
