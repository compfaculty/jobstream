"""Operational visibility must not disclose private inputs or mutate workflows."""

import time
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from redis.exceptions import ConnectionError

from jobs.models import AgentExecution, IngestionRun, OperationalSnapshot, Source
from jobs.services import operations
from jobs.services.agent_gateway import AgentGateway, GatewayError

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(client, user):
    user.is_staff = True
    user.save()
    device = user.totpdevice_set.create(name="operations-test", confirmed=True)
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return client


@pytest.fixture
def execution(user, job, evaluation_profile):
    return AgentExecution.objects.create(
        owner=user,
        job=job,
        profile=evaluation_profile,
        key="operations-test",
        snapshot={"private": "NEVER-EXPORT-CV"},
        model="fake",
        result={"private": "NEVER-EXPORT-RESULT"},
    )


def test_authentication_staff_mfa_and_read_only(client, authenticated, user):
    assert authenticated.get("/operations/").status_code == 403
    user.is_staff = True
    user.save()
    assert authenticated.get("/operations/").url == "/account/two_factor/setup/"
    client.logout()
    assert client.get("/operations/").status_code == 302


def test_empty_and_read_only(staff):
    response = staff.get("/operations/")
    assert response.status_code == 200
    assert b"No records in this window" in response.content
    assert b"Queue depths unavailable" in response.content
    assert staff.post("/operations/").status_code == 405
    assert staff.get("/health/").json() == {"status": "ok"}


def test_history_filters_pagination_and_redaction(staff, execution):
    for i in range(27):
        AgentExecution.objects.create(
            owner=execution.owner,
            job=execution.job,
            profile=execution.profile,
            key=f"ops-{i}",
            snapshot={"private": "NEVER-EXPORT-CV"},
            model="fake",
            status="failed",
            error_code="agent_provider_rejected",
        )
    AgentExecution.objects.filter(pk=execution.pk).update(
        created_at=timezone.now() - timedelta(days=2)
    )
    response = staff.get("/operations/?status=failed&page=2")
    assert response.status_code == 200
    assert len(response.context["rows"]) == 2
    assert response.context["page"].paginator.count == 27
    assert b"NEVER-EXPORT" not in response.content
    assert b"agent_provider_rejected" in response.content
    response = staff.get("/operations/?kind=bad&hours=bad&status=bad&page=bad")
    assert response.status_code == 200
    assert response.context["kind"] == "evaluations"
    assert response.context["hours"] == "24"
    assert response.context["status"] == ""


def test_tokens_overdue_and_unavailable_detail(staff, execution, monkeypatch):
    execution.status = "running"
    execution.deadline = timezone.now() - timedelta(seconds=1)
    execution.usage = {"input_tokens": 12, "output_tokens": 0}
    execution.save()
    response = staff.get("/operations/")
    assert response.context["rows"][0]["overdue"]
    assert response.context["tokens"] == {"input_tokens": 12, "output_tokens": 0, "unknown": 0}
    monkeypatch.setattr(
        AgentGateway, "provider_calls", Mock(side_effect=GatewayError("unavailable"))
    )
    response = staff.get(f"/operations/executions/{execution.pk}/")
    assert response.status_code == 200
    assert b"Provider-call history unavailable" in response.content
    assert b"NEVER-EXPORT" not in response.content
    assert b"Output 0" in response.content
    execution.usage = {}
    execution.save()
    response = staff.get("/operations/")
    assert response.context["tokens"]["unknown"] == 1


def test_ingestion_preparation_delivery(staff, application):
    source = Source.objects.create(name="Synthetic", url="https://example.com/jobs", adapter="rss")
    run = IngestionRun.objects.create(source=source)
    IngestionRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(minutes=11))
    response = staff.get("/operations/?kind=ingestion")
    assert response.context["rows"][0]["overdue"]
    assert staff.get("/operations/?kind=preparation").status_code == 200
    response = staff.get("/operations/?kind=delivery")
    assert response.context["rows"][0]["id"] == application.pk
    assert application.body.encode() not in response.content


def test_snapshot_fresh_stale_missing_worker_and_scheduler():
    assert operations.snapshot_context()["state"] == "unavailable"
    snapshot = OperationalSnapshot.objects.create(
        key="infrastructure",
        payload={
            "scheduler_published_at": time.time(),
            "workers": [{"name": "synthetic", "queues": ["celery"]}],
            "queues": [{"name": "celery", "depth": 0}, {"name": "delivery", "depth": 3}],
        },
    )
    context = operations.snapshot_context()
    assert context["state"] == context["scheduler"] == "healthy"
    assert [q["worker_state"] for q in context["queues"]] == ["healthy", "unavailable"]
    snapshot.observed_at -= timedelta(minutes=3)
    snapshot.save()
    context = operations.snapshot_context()
    assert context["state"] == context["scheduler"] == "stale"
    assert all(q["worker_state"] == "stale" for q in context["queues"])


def test_collection_bounded_probes_and_priority_depth(monkeypatch):
    from config.celery import app

    redis = MagicMock()
    redis.pipeline.return_value.__enter__.return_value.execute.return_value = [
        True,
        str(time.time()).encode(),
        *([1, 2, 3, 4] * 5),
    ]
    monkeypatch.setattr(operations, "broker_client", lambda: redis)
    inspector = Mock()
    inspector.active.return_value = {
        "worker": [
            {"id": "task", "name": "jobs.tasks.ingest_source", "args": "NEVER", "kwargs": "NEVER"}
        ]
    }
    inspector.active_queues.return_value = {"worker": [{"name": "celery", "private": "NEVER"}]}
    inspect_mock = Mock(return_value=inspector)
    monkeypatch.setattr(app.control, "inspect", inspect_mock)
    payload = operations.collect_snapshot()
    assert payload["redis"] == "healthy"
    assert payload["active_tasks"][0]["task_id"] == "task"
    assert payload["queues"][0]["depth"] == 10
    assert payload["workers"] == [{"name": "worker", "queues": ["celery"]}]
    assert "NEVER" not in str(payload)
    assert inspect_mock.call_args.kwargs["timeout"] == 1
    redis.close.assert_called_once()


def test_collection_dependency_timeout(monkeypatch):
    from config.celery import app

    redis = MagicMock()
    redis.pipeline.side_effect = ConnectionError("NEVER-EXPORT-CREDENTIAL")
    monkeypatch.setattr(operations, "broker_client", lambda: redis)
    monkeypatch.setattr(app.control, "inspect", Mock(side_effect=TimeoutError("private")))
    payload = operations.collect_snapshot()
    assert payload["redis"] == payload["worker_probe"] == "unavailable"
    assert "private" not in str(payload)
    assert "NEVER" not in str(OperationalSnapshot.objects.get().payload)


def metadata(execution_id):
    return {
        "schema_version": 1,
        "execution_id": execution_id,
        "calls": [
            {
                "call_id": str(uuid.uuid4()),
                "execution_id": execution_id,
                "attempt_id": str(uuid.uuid4()),
                "model": "fake",
                "started_at": timezone.now().isoformat(),
                "finished_at": None,
                "latency_ms": None,
                "outcome": "running",
                "error_code": "",
                "input_tokens": None,
                "output_tokens": None,
                "provider_request_id": "req_123",
                "private": "NEVER-EXPORT-CV",
            }
        ],
    }


def mock_gateway(monkeypatch, payload=None, status=200):
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))


def test_gateway_metadata_allowlist_and_unknown_tokens(monkeypatch, agent_settings):
    execution_id = str(uuid.uuid4())
    mock_gateway(monkeypatch, metadata(execution_id))
    calls = AgentGateway().provider_calls(execution_id)
    assert calls[0]["input_tokens"] is None
    assert "private" not in calls[0]


@pytest.mark.parametrize(
    "change",
    [
        {"input_tokens": -1},
        {"input_tokens": True},
        {"attempt_id": "wrong"},
        {"provider_request_id": "secret\nbody"},
        {"outcome": "invented"},
        {"execution_id": str(uuid.uuid4())},
        {"started_at": "2026-11-05"},
    ],
)
def test_invalid_provider_metadata_fails_closed(monkeypatch, agent_settings, change):
    execution_id = str(uuid.uuid4())
    payload = metadata(execution_id)
    payload["calls"][0].update(change)
    mock_gateway(monkeypatch, payload)
    with pytest.raises(GatewayError):
        AgentGateway().provider_calls(execution_id)


def test_gateway_provider_unavailable(monkeypatch, agent_settings):
    mock_gateway(monkeypatch, status=503)
    with pytest.raises(GatewayError):
        AgentGateway().provider_calls(str(uuid.uuid4()))


def test_task_lifecycle_logs_are_metadata_only(execution, caplog):
    from jobs.task_telemetry import finished, started

    task = Mock(name="task")
    task.name = "jobs.tasks.evaluate_vacancy"
    with caplog.at_level("INFO", logger="jobs.task_telemetry"):
        started(sender=task, task_id="task-id", args=(str(execution.pk),), kwargs={})
        finished(sender=task, task_id="task-id", state="SUCCESS", retval={"private": "NEVER"})
    assert [r.message for r in caplog.records] == ["task_started", "task_finished"]
    assert caplog.records[0].execution_id == str(execution.pk)
    assert caplog.records[0].attempt_id == str(execution.attempt_id)
    assert "NEVER" not in caplog.text


def test_provider_detail_renders_unknown_and_escapes_metadata(staff, execution, monkeypatch):
    call = metadata(str(execution.pk))["calls"][0]
    call.pop("private")
    call["model"] = "<script>synthetic</script>"
    monkeypatch.setattr(AgentGateway, "provider_calls", lambda *args: [call])
    response = staff.get(f"/operations/executions/{execution.pk}/")
    assert response.status_code == 200
    assert b"Input Unknown" in response.content
    assert b"req_123" in response.content
    assert b"<script>synthetic</script>" not in response.content
    assert b"&lt;script&gt;synthetic&lt;/script&gt;" in response.content
    assert staff.post(f"/operations/executions/{execution.pk}/").status_code == 405
