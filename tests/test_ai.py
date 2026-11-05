import json
from unittest.mock import Mock

import httpx
import pytest

from jobs.models import AIQuota
from jobs.services.ai import AIUnavailable, request_json, select_facts
from jobs.services.resumes import confirm
from tests.test_batches import document  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def ai_settings(settings):
    settings.OPENAI_API_KEY = "test-key-never-real"
    settings.OPENAI_MODEL = "test-model"
    settings.AI_MODEL_PRICES = {**settings.AI_MODEL_PRICES, "test-model": ["0.10", "0.50"]}


def mock_provider(monkeypatch, handler):
    client_class = httpx.Client
    monkeypatch.setattr(
        "jobs.services.ai.httpx.Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler)),
    )


def completed(result):
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(result)}],
                }
            ],
        },
    )


def test_bounded_cached_request(user, ai_settings, monkeypatch):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        assert payload["store"] is False
        assert payload["model"] == "test-model"
        assert payload["text"]["format"]["strict"] is True
        calls.append(payload)
        return completed({"answer": "ok"})

    mock_provider(monkeypatch, handler)
    assert request_json(user.pk, "test", {}, {}, "instructions") == {"answer": "ok"}
    assert request_json(user.pk, "test", {}, {}, "instructions") == {"answer": "ok"}
    assert len(calls) == 1
    assert AIQuota.objects.get().reserved_tokens > 0


def test_provider_rate_limit_deferred(user, ai_settings, monkeypatch):
    mock_provider(monkeypatch, lambda request: httpx.Response(429, headers={"Retry-After": "600"}))
    with pytest.raises(AIUnavailable) as error:
        request_json(user.pk, "test", {}, {}, "instructions")
    assert error.value.delay == 600
    with pytest.raises(AIUnavailable, match="ai_retry_scheduled"):
        request_json(user.pk, "test", {}, {}, "instructions")


def test_quota_blocks_network(user, ai_settings, settings, monkeypatch):
    settings.AI_DAILY_TOKEN_BUDGET = 1
    client = Mock()
    monkeypatch.setattr("jobs.services.ai.httpx.Client", client)
    with pytest.raises(AIUnavailable, match="ai_daily_budget_exceeded"):
        request_json(user.pk, "test", {}, {}, "instructions")
    client.assert_not_called()


def test_reject_invented_fact_and_gap(user, document, monkeypatch):  # noqa: F811
    profile = confirm(user, document, "Alice", "", {"Skills": "Python"})
    monkeypatch.setattr(
        "jobs.services.ai.request_json",
        lambda *args: {
            "ordered_fact_ids": ["invented"],
            "highlight_fact_ids": [],
            "unmatched_requirements": [],
        },
    )
    with pytest.raises(AIUnavailable, match="ai_unsupported_claim"):
        select_facts(profile, {"title": "Engineer", "description": "Python required"})
    monkeypatch.setattr(
        "jobs.services.ai.request_json",
        lambda *args: {
            "ordered_fact_ids": ["fact-1"],
            "highlight_fact_ids": ["fact-1"],
            "unmatched_requirements": ["Invented qualification"],
        },
    )
    with pytest.raises(AIUnavailable, match="ai_unsupported_claim"):
        select_facts(profile, {"title": "Engineer", "description": "Python required"})


def test_prompt_and_model_parameters_invalidate_cache(user, ai_settings, monkeypatch):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 30, "output_tokens": 10},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"answer":"ok"}'}],
                    }
                ],
            },
        )

    mock_provider(monkeypatch, handler)
    request_json(
        user.pk,
        "test",
        {},
        {},
        "one",
        model="gpt-6-luna",
        reasoning_effort="none",
        max_output_tokens=100,
    )
    request_json(
        user.pk,
        "test",
        {},
        {},
        "one",
        model="gpt-6-luna",
        reasoning_effort="none",
        max_output_tokens=100,
    )
    request_json(
        user.pk,
        "test",
        {},
        {},
        "two",
        model="gpt-6-luna",
        reasoning_effort="low",
        max_output_tokens=100,
    )
    assert len(requests) == 2
    assert requests[0]["model"] == "gpt-6-luna"
    assert requests[0]["reasoning"] == {"effort": "none"}
    assert requests[0]["max_output_tokens"] == 100
    assert AIQuota.objects.get().reserved_tokens == 80


def test_ai_output_limit_zero_is_rejected_before_network(user, ai_settings, monkeypatch):
    client = Mock()
    monkeypatch.setattr("jobs.services.ai.httpx.Client", client)
    with pytest.raises(AIUnavailable, match="ai_invalid_output_budget"):
        request_json(user.pk, "test", {}, {}, "instructions", max_output_tokens=0)
    client.assert_not_called()
