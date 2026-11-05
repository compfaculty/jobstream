import io
import json
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from jobs.models import (
    AICharge,
    AICostMonth,
    AIQuota,
    Job,
    Source,
    UserComparisonDay,
)
from jobs.services import normalization_batch as batches
from jobs.services.ai_costs import BudgetUnavailable, reserve, settle
from jobs.services.cv_profiles import build, ensure, exact_assessments

pytestmark = pytest.mark.django_db


def test_monthly_and_daily_limits_are_atomic(settings, user):
    settings.AI_MONTHLY_BUDGET_USD = "0.001"
    settings.AI_DAILY_TOKEN_BUDGET = 100000
    first = reserve("one", "gpt-6-luna", "test", 1000, 1000, owner=user, daily=True)
    assert reserve("one", "gpt-6-luna", "test", 1000, 1000, owner=user, daily=True).pk == first.pk
    assert AIQuota.objects.get().reserved_tokens == 2000
    with pytest.raises(BudgetUnavailable, match="monthly"):
        reserve("two", "gpt-6-luna", "test", 1000, 1000, daily=True)
    assert AIQuota.objects.get().reserved_tokens == 2000
    assert settle(first.pk, {"input_tokens": 100, "output_tokens": 50})
    assert not settle(first.pk, {"input_tokens": 100, "output_tokens": 50})
    assert AIQuota.objects.get().reserved_tokens == 150
    assert AICostMonth.objects.get().committed_usd == Decimal("0.000035")


def test_user_slots_and_unknown_outcomes(settings, user):
    settings.AI_USER_DAILY_COMPARISONS = 1
    ticket = reserve("one", "gpt-6-luna", "comparison", 10, 10, owner=user, comparison=True)
    assert not settle(ticket.pk, {})
    with pytest.raises(BudgetUnavailable, match="user_daily"):
        reserve("two", "gpt-6-luna", "comparison", 10, 10, owner=user, comparison=True)
    assert AICharge.objects.count() == UserComparisonDay.objects.get().admitted == 1
    user.is_staff = True
    reserve("staff", "gpt-6-luna", "comparison", 10, 10, owner=user, comparison=True)
    with pytest.raises(BudgetUnavailable, match="price_unknown"):
        reserve("unknown", "unpriced-model", "test", 10, 10)


def test_reset_is_read_only_by_default(user, job):
    user.is_superuser = True
    user.save()
    out = io.StringIO()
    call_command("reset_pilot", admin=user.username, stdout=out)
    assert Job.objects.filter(pk=job.pk).exists()
    assert json.loads(out.getvalue())["apply"] is False
    with pytest.raises(CommandError, match="verified backup"):
        call_command("reset_pilot", admin=user.username, apply=True, stdout=io.StringIO())


def test_profile_grounding_cache_and_owner_isolation(settings, user, document, monkeypatch):  # noqa: F811
    settings.OPENAI_MODEL = "gpt-6-luna"
    monkeypatch.setattr(
        "jobs.services.resumes.extract",
        lambda d: "Python\nBuilt Linux services\nNo Java experience",
    )
    monkeypatch.setattr("jobs.services.ai.request_json", lambda *a, **kw: {"lines": [0, 1, 2]})
    record = ensure(document)
    build(record.pk)
    record.refresh_from_db()
    assert record.state == "ready"
    assert ensure(document).pk == record.pk
    monkeypatch.setattr(
        "jobs.services.resumes.extract", lambda d: pytest.fail("PDF extracted twice")
    )
    build(record.pk)
    assert (
        exact_assessments(
            [
                {"id": "r1", "text": "Python"},
                {"id": "r2", "text": "Java"},
                {"id": "r3", "text": "5 years Python"},
            ],
            record.facts,
        )[0]["requirement_id"]
        == "r1"
    )
    from django.contrib.auth import get_user_model

    other = get_user_model().objects.create_user(username="profile-other")
    document.owner = other
    assert ensure(document).pk != record.pk


def test_profile_rejects_fabricated_evidence(settings, document, monkeypatch):  # noqa: F811
    monkeypatch.setattr("jobs.services.resumes.extract", lambda d: "Python")
    monkeypatch.setattr("jobs.services.ai.request_json", lambda *a, **kw: {"lines": [99]})
    record = ensure(document)
    build(record.pk)
    record.refresh_from_db()
    assert record.state == "fallback"
    assert record.facts[0]["text"] == "Python"


def make_work(job):
    job.description = "We build a platform.\nPython required.\nBuild services."
    job.normalized = {}
    job.save()
    return batches.enqueue(job)


def success_row(work):
    return json.dumps(
        {
            "custom_id": work.key,
            "response": {
                "status_code": 200,
                "body": {
                    "status": "completed",
                    "usage": {"input_tokens": 100, "output_tokens": 30},
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(
                                        {
                                            "summary": [2],
                                            "product": [0],
                                            "requirements": [1],
                                            "preferred": [],
                                            "responsibilities": [2],
                                        }
                                    ),
                                }
                            ],
                        }
                    ],
                },
            },
        }
    )


def test_batch_success_duplicate_poll_and_stale_job(settings, job, monkeypatch):
    settings.JOB_NORMALIZATION_MODEL = "gpt-6-luna"
    work = make_work(job)
    assert batches.enqueue(job).pk == work.pk
    batch = batches.prepare()
    calls = []

    def api(method, path, **kw):
        calls.append((method, path))
        if path == "/files":
            return {"id": "file-input"}
        if path == "/batches" and method == "POST":
            return {"id": "batch-test", "status": "validating"}
        if path == "/batches/batch-test":
            return {"status": "completed", "output_file_id": "file-output"}
        if path == "/files/file-output/content":
            return success_row(work)
        raise AssertionError(path)

    monkeypatch.setattr(batches, "api", api)
    batches.submit(batch)
    batches.submit(batch)
    assert calls.count(("POST", "/batches")) == 1
    batches.poll(batch)
    batches.poll(batch)
    job.refresh_from_db()
    assert job.normalized["requirements"] == ["Python required."]
    assert AICharge.objects.get().state == "settled"
    job.description = "Changed source"
    job.save()
    work.refresh_from_db()
    batches.apply_result(work)
    job.refresh_from_db()
    assert job.description == "Changed source"


def test_batch_unknown_submission_not_repeated(settings, job, monkeypatch):
    from jobs.services.ai import AIUnavailable

    settings.JOB_NORMALIZATION_MODEL = "gpt-6-luna"
    make_work(job)
    batch = batches.prepare()
    monkeypatch.setattr(batches, "api", lambda *a, **kw: (_ for _ in ()).throw(AIUnavailable()))
    batches.submit(batch)
    batch.refresh_from_db()
    assert batch.state == "unknown"
    monkeypatch.setattr(batches, "api", lambda *a, **kw: pytest.fail("resubmitted ambiguous batch"))
    batches.submit(batch)
    assert AICharge.objects.get().state == "reserved"


def test_batch_override_and_budget_block(settings, job):
    settings.JOB_NORMALIZATION_MODEL = "gpt-6-luna"
    work = make_work(job)
    settings.AI_MONTHLY_BUDGET_USD = "0"
    assert batches.prepare() is None
    work.refresh_from_db()
    assert work.state == "budget_blocked"
    job.content_overrides = {"description": job.description}
    assert batches.enqueue(job) is None


from tests.test_batches import document  # noqa: E402,F401


@pytest.mark.django_db(transaction=True)
def test_reset_preserves_auth_sources_and_budget(tmp_path, user, job, settings):
    import hashlib

    from django.contrib.auth import get_user_model
    from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
    from django_otp.plugins.otp_totp.models import TOTPDevice

    user.is_superuser = user.is_staff = True
    user.save()
    password = user.password
    totp = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    static = StaticDevice.objects.create(user=user, name="backup")
    token = StaticToken.objects.create(device=static, token="test-recovery")
    other = get_user_model().objects.create_user(username="reset-other")
    source = Source.objects.create(
        name="retained",
        url="https://example.com",
        adapter="rss",
        enabled=True,
        inventory_cursor={"page": 5},
        failure_count=2,
    )
    reserve("before-reset", "gpt-6-luna", "test", 100, 100, owner=user)
    amount = AICostMonth.objects.get().committed_usd
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"test-backup-not-a-real-database")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "restore_verified": True,
                "admin_id": user.pk,
                "database_file": dump.name,
                "database_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
                "files": [],
            }
        )
    )
    call_command(
        "reset_pilot",
        admin=user.username,
        apply=True,
        quiesced=True,
        backup_manifest=str(manifest),
        stdout=io.StringIO(),
    )
    user.refresh_from_db()
    source.refresh_from_db()
    assert user.password == password and user.is_superuser
    assert TOTPDevice.objects.get(pk=totp.pk).key == totp.key
    assert StaticToken.objects.get(pk=token.pk).token == "test-recovery"
    assert not get_user_model().objects.filter(pk=other.pk).exists()
    assert not Job.objects.exists() and not AICharge.objects.exists()
    assert source.enabled and source.inventory_cursor == {} and source.failure_count == 0
    assert AICostMonth.objects.get().carried_usd == amount


def test_5000_unique_job_events_are_visible_and_batch_preparation_is_bounded(settings):
    settings.JOB_NORMALIZATION_MODEL = "gpt-6-luna"
    settings.AI_DAILY_TOKEN_BUDGET = 10000000
    settings.JOB_NORMALIZATION_BATCH_SIZE = 50
    jobs = Job.objects.bulk_create(
        [
            Job(
                canonical_url=f"https://example.com/capacity/{i}",
                title="Engineer",
                description=f"Build platform {i}. Python required.",
                normalized={},
            )
            for i in range(5000)
        ]
    )
    for job in jobs:
        batches.enqueue(job)
    assert Job.objects.count() == 5000
    from jobs.models import NormalizationWork

    assert NormalizationWork.objects.count() == 5000
    for job in jobs[:100]:
        batches.enqueue(job)
    assert NormalizationWork.objects.count() == 5000
    batch = batches.prepare()
    assert batch.normalizationwork_set.count() == 50
    assert NormalizationWork.objects.filter(state="pending").count() == 4950
    assert Job.objects.count() == 5000


@pytest.mark.parametrize("terminal", ["expired", "cancelled", "failed"])
def test_terminal_batch_retains_unknown_usage_and_retries_proven_no_run(
    settings, job, monkeypatch, terminal
):
    settings.JOB_NORMALIZATION_MODEL = "gpt-6-luna"
    work = make_work(job)
    batch = batches.prepare()
    batch.provider_id, batch.state = "batch-terminal", "in_progress"
    batch.save()
    raw = json.dumps({"custom_id": work.key, "response": None, "error": {"code": "batch_expired"}})

    def api(method, path, **kwargs):
        if path.startswith("/batches/"):
            return {"status": terminal, "error_file_id": "file-error"}
        return raw

    monkeypatch.setattr(batches, "api", api)
    batches.poll(batch)
    work.refresh_from_db()
    assert work.state == "retry" and work.attempts == 1
    assert AICharge.objects.get().charged_usd == 0
    batches.poll(batch)
    assert AICharge.objects.count() == 1


def test_batch_recovery_finds_lost_create_response(settings, job, monkeypatch):
    settings.JOB_NORMALIZATION_MODEL = "gpt-6-luna"
    make_work(job)
    batch = batches.prepare()
    batch.state = "unknown"
    batch.save()

    def api(method, path, **kwargs):
        assert method == "GET"
        if path == "/batches":
            return {
                "data": [{"id": "batch-recovered", "metadata": {"jobstream_batch": str(batch.pk)}}]
            }
        return {"status": "in_progress"}

    monkeypatch.setattr(batches, "api", api)
    batches.poll(batch)
    batch.refresh_from_db()
    assert batch.provider_id == "batch-recovered" and batch.state == "in_progress"


def test_normalization_api_timeout_is_safe(monkeypatch):
    import httpx

    from jobs.services.ai import AIUnavailable

    class TimeoutClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            raise httpx.ReadTimeout("do not log this secret")

    monkeypatch.setattr(batches.httpx, "Client", TimeoutClient)
    from django.test import override_settings

    with override_settings(OPENAI_API_KEY="test-no-real-key"):
        with pytest.raises(AIUnavailable, match="ai_batch_unavailable"):
            batches.api("GET", "/batches")


@pytest.mark.django_db(transaction=True)
def test_concurrent_reservations_cannot_bypass_monthly_cap(settings):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections, connections

    settings.AI_MONTHLY_BUDGET_USD = "0.00012"

    def admit(index):
        close_old_connections()
        try:
            reserve(f"concurrent-{index}", "gpt-6-luna", "test", 100, 100)
            return True
        except BudgetUnavailable:
            return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(admit, range(8)))
    assert sum(outcomes) == 2
    assert AICharge.objects.count() == 2
    assert AICostMonth.objects.get().committed_usd == Decimal("0.00012")


def test_new_profile_flow_requires_ready_and_deterministic_hit_is_free(  # noqa: F811
    settings,
    document,  # noqa: F811
    job,
    user,
    agent_settings,  # noqa: F811
):  # noqa: F811
    from django.core.exceptions import ValidationError

    from jobs.services import evaluations

    settings.CV_REPRESENTATIONS_ENABLED = True
    with pytest.raises(ValidationError, match="being processed"):
        evaluations.start(user, job, document_id=document.pk)
    record = ensure(document)
    record.state = "ready"
    record.facts = [{"id": "line-0", "text": "Python", "section": "Other", "source_line": 0}]
    record.save()
    job.normalized = {"requirements": ["Python"]}
    job.save()
    execution = evaluations.start(user, job, document_id=document.pk)
    evaluations.process(execution.pk)
    execution.refresh_from_db()
    assert execution.status == "completed" and execution.result["match_score"] == 100
    assert not AICharge.objects.exists() and not UserComparisonDay.objects.exists()
    assert evaluations.start(user, job, document_id=document.pk).pk == execution.pk


def test_batch_request_is_immutable_after_enqueue(settings, job):
    settings.JOB_NORMALIZATION_MODEL = "gpt-6-luna"
    settings.JOB_NORMALIZATION_REASONING_EFFORT = "none"
    work = make_work(job)
    expected = batches.request_body(work)
    settings.JOB_NORMALIZATION_REASONING_EFFORT = "low"
    settings.JOB_NORMALIZATION_MODEL = "unpriced-model"
    work.refresh_from_db()
    assert batches.request_body(work) == expected
    assert expected["reasoning"]["effort"] == "none"


def test_unapproved_rollout_never_submits_paid_batches(settings, monkeypatch):
    from jobs.tasks import submit_normalization_batch

    settings.JOB_NORMALIZATION_BATCH_ENABLED = True
    settings.AI_PILOT_ROLLOUT_APPROVED = False
    monkeypatch.setattr(batches, "prepare", lambda: pytest.fail("unapproved paid admission"))
    submit_normalization_batch()


def test_exact_skill_aliases_do_not_match_negation_or_experience():
    facts = [
        {"id": "f1", "text": "Postgres"},
        {"id": "f2", "text": "No Java experience"},
        {"id": "f3", "text": "go"},
    ]
    criteria = [
        {"id": "r1", "text": "PostgreSQL"},
        {"id": "r2", "text": "Java"},
        {"id": "r3", "text": "Go"},
    ]
    result = exact_assessments(criteria, facts)
    assert [r["requirement_id"] for r in result] == ["r1"]
    assert result[0]["evidence_quote"] == "Postgres"


def test_profile_dispatch_claim_can_be_consumed_immediately(settings, document, monkeypatch):  # noqa: F811
    from jobs.tasks import dispatch_cv_profiles

    settings.CV_REPRESENTATIONS_ENABLED = True
    record = ensure(document)
    monkeypatch.setattr("jobs.tasks.build_cv_profile.delay", lambda pk: build(pk))
    monkeypatch.setattr("jobs.services.resumes.extract", lambda d: "Python")
    monkeypatch.setattr("jobs.services.ai.request_json", lambda *a, **kw: {"lines": [0]})
    dispatch_cv_profiles()
    record.refresh_from_db()
    assert record.state == "ready" and record.lease_until is None
