import uuid
from datetime import timedelta
from unittest.mock import Mock

import httpx
import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from jobs.models import AgentExecution, AIQuota, Application, AuditEvent
from jobs.services import evaluations
from jobs.services.agent_gateway import AgentGateway, ExecutionResponse, GatewayError
from jobs.tasks import dispatch_evaluations

pytestmark = pytest.mark.django_db


def output():
    return {"ordered_fact_ids": ["f1"], "highlight_fact_ids": ["f1"], "unmatched_requirements": []}


def response(record, status="completed", result=None, **kwargs):
    return ExecutionResponse(
        str(record.pk),
        str(record.attempt_id),
        str(record.pk),
        status,
        output() if result is None else result,
        {"input_tokens": 10, "output_tokens": 5},
        "",
        False,
        0,
        **kwargs,
    )


def test_owner_snapshot_dedup_and_disabled(
    user, other, job, evaluation_profile, agent_settings, settings
):
    record = evaluations.start(user, job, evaluation_profile.pk)
    assert evaluations.start(user, job, evaluation_profile.pk).pk == record.pk
    job.description = "changed"
    job.save()
    assert record.snapshot["job"]["description"] != job.description
    assert evaluations.start(user, job, evaluation_profile.pk).pk != record.pk
    with pytest.raises(ValidationError):
        evaluations.start(other, job, evaluation_profile.pk)
    settings.AGENT_BACKEND = "disabled"
    with pytest.raises(ValidationError):
        evaluations.start(user, job, evaluation_profile.pk)


def test_success_duplicate_and_budget(user, job, evaluation_profile, agent_settings, monkeypatch):
    record = evaluations.start(user, job, evaluation_profile.pk)
    start = Mock(return_value=response(record))
    monkeypatch.setattr(AgentGateway, "start", start)
    evaluations.process(record.pk)
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert record.status == "completed"
    assert record.result["fit"] == record.snapshot["fit"]
    assert record.usage == {"input_tokens": 10, "output_tokens": 5}
    assert start.call_count == 1
    assert AIQuota.objects.get().reserved_tokens > 0
    assert not Application.objects.exists()
    assert AuditEvent.objects.filter(event="agent.completed").count() == 1


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {},
        {"ordered_fact_ids": ["invented"], "highlight_fact_ids": [], "unmatched_requirements": []},
        {
            "ordered_fact_ids": ["f1"],
            "highlight_fact_ids": ["f1", "f1"],
            "unmatched_requirements": [],
        },
        {
            "ordered_fact_ids": ["f1"],
            "highlight_fact_ids": [],
            "unmatched_requirements": ["invented requirement"],
        },
    ],
)
def test_invalid_proposals(evaluation_profile, user, job, agent_settings, result):
    record = evaluations.start(user, job, evaluation_profile.pk)
    with pytest.raises(GatewayError):
        evaluations.validate_result(record.snapshot, result)


def test_invalid_output_fails_closed(user, job, evaluation_profile, agent_settings, monkeypatch):
    record = evaluations.start(user, job, evaluation_profile.pk)
    monkeypatch.setattr(AgentGateway, "start", lambda *args: response(record, result={}))
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert record.status == "failed"
    assert record.error_code == "agent_invalid_result"
    assert not record.result


def test_cancel_and_late_result_fenced(user, job, evaluation_profile, agent_settings, monkeypatch):
    record = evaluations.start(user, job, evaluation_profile.pk)
    monkeypatch.setattr(AgentGateway, "cancel", Mock())

    def complete(*args):
        evaluations.cancel(record)
        return response(record)

    monkeypatch.setattr(AgentGateway, "start", complete)
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert record.status == "cancelled"
    assert not record.result
    assert record.cancellation_delivered


def test_stale_attempt_fenced(user, job, evaluation_profile, agent_settings, monkeypatch):
    record = evaluations.start(user, job, evaluation_profile.pk)

    def complete(*args):
        AgentExecution.objects.filter(pk=record.pk).update(attempt_id=uuid.uuid4())
        return response(record)

    monkeypatch.setattr(AgentGateway, "start", complete)
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert record.status == "running"
    assert not record.result


def test_transport_ambiguity_same_attempt_and_timeout(
    user, job, evaluation_profile, agent_settings, monkeypatch
):
    record = evaluations.start(user, job, evaluation_profile.pk)

    def unavailable(*args):
        raise GatewayError("agent_unavailable", True)

    monkeypatch.setattr(AgentGateway, "start", unavailable)
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert record.status == "running" and record.attempt == 1
    AgentExecution.objects.filter(pk=record.pk).update(
        deadline=timezone.now() - timedelta(seconds=1)
    )
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert record.status == "failed" and record.retryable
    AgentExecution.objects.filter(pk=record.pk).update(next_dispatch_at=timezone.now())
    evaluations.retry(record)
    record.refresh_from_db()
    assert record.status == "queued" and record.attempt == 2
    assert record.deadline is None


def test_quota_rejected_before_network(
    user, job, evaluation_profile, agent_settings, settings, monkeypatch
):
    settings.AI_DAILY_TOKEN_BUDGET = 1
    start = Mock()
    monkeypatch.setattr(AgentGateway, "start", start)
    record = evaluations.start(user, job, evaluation_profile.pk)
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert record.error_code == "ai_daily_budget_exceeded"
    start.assert_not_called()


def test_context_scope_revocation_and_ui(
    user, other, job, evaluation_profile, agent_settings, client
):
    record = evaluations.start(user, job, evaluation_profile.pk)
    record.status = "running"
    record.deadline = timezone.now() + timedelta(seconds=180)
    record.save()
    url = reverse("evaluation-context", args=[record.pk, record.attempt_id])
    token = evaluations.capability(record)
    assert client.get(url).status_code == 404
    assert client.get(url, HTTP_AUTHORIZATION="Bearer " + token).status_code == 200
    assert (
        client.get(
            reverse("evaluation-context", args=[record.pk, uuid.uuid4()]),
            HTTP_AUTHORIZATION="Bearer " + token,
        ).status_code
        == 404
    )
    client.force_login(other)
    assert ">Confirmed</option>" not in client.get(reverse("job", args=[job.pk])).content.decode()
    assert client.post(reverse("evaluation-action", args=[record.pk, "cancel"])).status_code == 404
    assert (
        str(record.pk)
        not in client.get(reverse("evaluation-panel", args=[job.pk])).content.decode()
    )
    record.status = "cancelled"
    record.save()
    assert client.get(url, HTTP_AUTHORIZATION="Bearer " + token).status_code == 404


def test_post_requires_csrf(user, job, evaluation_profile, agent_settings):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert (
        client.post(
            reverse("evaluation-start", args=[job.pk]), {"profile": evaluation_profile.pk}
        ).status_code
        == 403
    )


def test_lost_publication_recovers(user, job, evaluation_profile, agent_settings, monkeypatch):
    record = evaluations.start(user, job, evaluation_profile.pk)
    publish = Mock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr("jobs.tasks.evaluate_vacancy.delay", publish)
    with pytest.raises(RuntimeError):
        dispatch_evaluations()
    record.refresh_from_db()
    assert record.status == "queued"
    AgentExecution.objects.filter(pk=record.pk).update(next_dispatch_at=timezone.now())
    publish.side_effect = None
    dispatch_evaluations()
    assert publish.call_count == 2


def test_gateway_contract_and_auth(agent_settings, monkeypatch):
    execution_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
    data = {
        "schema_version": 1,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "correlation_id": execution_id,
        "status": "running",
        "result": {},
        "usage": {},
        "error_code": "",
        "retryable": False,
        "retry_after": 0,
    }
    client_class = httpx.Client

    def handler(request):
        assert request.headers["Authorization"].startswith("Bearer test-service")
        return httpx.Response(200, json=data)

    monkeypatch.setattr(
        "jobs.services.agent_gateway.httpx.Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler)),
    )
    assert AgentGateway().status(execution_id, attempt_id).status == "running"
    data["attempt_id"] = str(uuid.uuid4())
    with pytest.raises(GatewayError):
        AgentGateway().status(execution_id, attempt_id)
    data["attempt_id"] = attempt_id
    data["usage"] = {"input_tokens": True}
    with pytest.raises(GatewayError):
        AgentGateway().status(execution_id, attempt_id)


def test_retry_window_and_attempt_limit(user, job, evaluation_profile, agent_settings):
    record = evaluations.start(user, job, evaluation_profile.pk)
    record.status, record.retryable = "failed", True
    record.next_dispatch_at = timezone.now() + timedelta(seconds=60)
    record.save()
    with pytest.raises(ValidationError, match="retry window"):
        evaluations.retry(record)
    record.next_dispatch_at, record.attempt = timezone.now(), 3
    record.save()
    with pytest.raises(ValidationError):
        evaluations.retry(record)


def test_capability_expiry_and_escaped_evidence(
    user, job, evaluation_profile, agent_settings, client, monkeypatch
):
    import time
    from unittest.mock import patch

    from django.core import signing

    job.description = "Python <script>alert(1)</script>"
    job.save()
    record = evaluations.start(user, job, evaluation_profile.pk)
    record.status, record.deadline = "running", timezone.now() + timedelta(seconds=180)
    record.save()
    with patch(
        "django.core.signing.TimestampSigner.timestamp",
        return_value=signing.b62_encode(int(time.time()) - 181),
    ):
        expired = evaluations.capability(record)
    assert (
        client.get(
            reverse("evaluation-context", args=[record.pk, record.attempt_id]),
            HTTP_AUTHORIZATION="Bearer " + expired,
        ).status_code
        == 404
    )
    result = output()
    result["unmatched_requirements"] = ["<script>alert(1)</script>"]
    monkeypatch.setattr(AgentGateway, "start", lambda *args: response(record, result=result))
    monkeypatch.setattr(AgentGateway, "status", lambda *args: response(record, result=result))
    evaluations.process(record.pk)
    client.force_login(user)
    html = client.get(reverse("evaluation-panel", args=[job.pk])).content.decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_completed_result_recovers_after_worker_deadline(
    user, job, evaluation_profile, agent_settings, monkeypatch
):
    record = evaluations.start(user, job, evaluation_profile.pk)
    record.status = "running"
    record.deadline = timezone.now() - timedelta(seconds=1)
    record.save()
    monkeypatch.setattr(AgentGateway, "status", lambda *args: response(record))
    start = Mock()
    monkeypatch.setattr(AgentGateway, "start", start)
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert record.status == "completed"
    start.assert_not_called()


def test_disabled_backend_stops_polling(
    user, job, evaluation_profile, agent_settings, client, settings
):
    evaluations.start(user, job, evaluation_profile.pk)
    settings.AGENT_BACKEND = "disabled"
    client.force_login(user)
    html = client.get(reverse("evaluation-panel", args=[job.pk])).content.decode()
    assert 'hx-trigger="every 5s"' not in html
    assert "Retry evaluation" not in html
