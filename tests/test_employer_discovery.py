import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock

import httpx
import pytest
from django.apps import apps
from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.test import RequestFactory
from django.utils import timezone

from jobs.admin_discovery import EmployerDiscoveryLeadAdmin
from jobs.models import (
    Application,
    Company,
    CrawlHost,
    EmployerDiscoveryBudget,
    EmployerDiscoveryCampaign,
    EmployerDiscoveryLead,
    EmployerDiscoveryRun,
    Job,
    Source,
)
from jobs.services import brave_search
from jobs.services import employer_discovery as discovery
from jobs.services import employer_evidence as evidence
from jobs.services.crawl import CrawlDeferred, collection_context
from jobs.services.employer_defaults import discovery_queries, technology_roles
from jobs.services.network import HTTPStatusError, UnsafeURL

pytestmark = pytest.mark.django_db
HOME = "https://acme.example/"
JOB = HOME + "jobs/1"
BOARD = "https://job-boards.greenhouse.io/acme"
GH = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"


@pytest.fixture
def campaign(settings):
    settings.BRAVE_SEARCH_API_KEY = "fake-brave-key"
    return EmployerDiscoveryCampaign.objects.create(
        name="Test discovery",
        enabled=True,
        queries=["Israel software careers", "Ukraine software jobs"],
        technology_roles=technology_roles(),
    )


def node(**kwargs):
    return {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Engineer",
        "description": "Build software",
        "datePosted": "2026-01-01",
        "url": JOB,
        "hiringOrganization": {"@type": "Organization", "name": "Acme", "url": HOME},
        "jobLocation": {"address": {"addressLocality": "Berlin", "addressCountry": "DE"}},
        **kwargs,
    }


def html(value, links=""):
    return (
        '<script type="application/ld+json">' + json.dumps(value) + "</script>" + links
    ).encode()


def patch_pages(monkeypatch, pages):
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return value, url

    monkeypatch.setattr(evidence, "fetch_public_document", fetch)
    return calls


def lead_for(campaign):
    run = discovery.start(campaign.pk)
    return EmployerDiscoveryLead.objects.create(
        campaign=campaign,
        run=run,
        key="a" * 64,
        candidate_url=JOB,
        search_query="public query",
        name="Acme",
        website=HOME,
        career_url=HOME + "jobs",
        vacancy_title="Software Engineer",
        vacancy_location="Berlin, Germany",
        vacancy_url=JOB,
        relationship_url=HOME,
        observed_at=timezone.now(),
        evidence_kind="manual",
    )


def ready(run):
    EmployerDiscoveryRun.objects.filter(pk=run.pk).update(next_attempt_at=timezone.now())


def test_seed_disabled_and_defaults():
    migration = importlib.import_module("jobs.migrations.0049_seed_employer_discovery")
    migration.seed(apps, Mock(connection=connection))
    migration.seed(apps, Mock(connection=connection))
    seed = EmployerDiscoveryCampaign.objects.get(name="Regional technology employers")
    assert not seed.enabled
    assert seed.queries == discovery_queries()
    assert seed.technology_roles == technology_roles()
    assert len(seed.queries) == 29
    assert not EmployerDiscoveryRun.objects.exists()


def test_full_worker_promotes_without_activation(campaign, monkeypatch):
    patch_pages(monkeypatch, {JOB: html(node())})
    search = Mock(return_value=([JOB, JOB, "http://localhost/jobs"], False))
    monkeypatch.setattr(brave_search, "search", search)
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    run.refresh_from_db()
    assert len(run.pending_leads) == 1
    assert run.duplicate == 1
    ready(run)
    discovery.step(run.pk)
    run.refresh_from_db()
    lead = EmployerDiscoveryLead.objects.get(campaign=campaign)
    company = Company.objects.get(pk=lead.company_id)
    source = Source.objects.get(company=company)
    assert run.confirmed == run.inspected == run.searched == 1
    assert lead.status == "confirmed" and lead.vacancy_location == "Berlin, Germany"
    assert (
        company.review_required and not company.website_verified and company.official_domain is None
    )
    assert not source.enabled and not source.authoritative and source.discovery_pending
    assert not Job.objects.exists() and not Application.objects.exists()
    assert search.call_args.args == (campaign.queries[0], 0)
    campaign.refresh_from_db()
    assert campaign.query_cursor == 1


def test_existing_company_preserved(campaign):
    lead = lead_for(campaign)
    assert discovery.promote(lead.pk)
    lead.refresh_from_db()
    company = lead.company
    company.description = "Curated"
    company.save()
    assert not discovery.promote(lead.pk)
    company.refresh_from_db()
    assert company.description == "Curated" and Company.objects.count() == 1
    assert Source.objects.count() == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "Account Executive"},
        {"jobLocation": {"address": {"addressCountry": "GB"}}},
        {"jobLocation": {"address": {"addressCountry": "US"}}},
        {"jobLocationType": "TELECOMMUTE"},
        {"validThrough": "2020-01-01"},
        {"validThrough": "bad-date"},
        {"status": "closed"},
        {"datePosted": "2099-01-01"},
        {"datePosted": None},
        {"description": ""},
        {"url": "https://unrelated.example/jobs/1"},
        {"hiringOrganization": {"name": "Acme"}},
    ],
)
def test_unconfirmed_evidence_stays_unresolved(monkeypatch, changes):
    patch_pages(monkeypatch, {JOB: html(node(**changes))})
    result, diagnostic = evidence.inspect(JOB, technology_roles())
    assert not result.evidence_kind and diagnostic


@pytest.mark.parametrize("country", ["IL", "UA", "PL", "Ireland", "European Union"])
def test_explicit_remote_eligibility(monkeypatch, country):
    value = node(jobLocationType="TELECOMMUTE", applicantLocationRequirements={"name": country})
    patch_pages(monkeypatch, {JOB: html(value)})
    result, _ = evidence.inspect(JOB, technology_roles())
    assert result.evidence_kind == "jsonld"


def test_visible_closed_page_is_not_current(monkeypatch):
    patch_pages(monkeypatch, {JOB: html(node(), "<p>No longer accepting applications</p>")})
    assert not evidence.inspect(JOB, technology_roles())[0].evidence_kind


def test_employer_linked_greenhouse(monkeypatch):
    org = {"@type": "Organization", "name": "Acme", "url": HOME}
    calls = patch_pages(
        monkeypatch,
        {
            HOME: html(org, f'<a href="{BOARD}">Careers</a>'),
            GH: json.dumps(
                {
                    "jobs": [
                        {
                            "id": 1,
                            "title": "Software Engineer",
                            "location": {"name": "Tel Aviv, Israel"},
                            "absolute_url": BOARD + "/jobs/1",
                            "content": "Build software",
                        }
                    ]
                }
            ).encode(),
        },
    )
    result, _ = evidence.inspect(HOME, technology_roles())
    assert result.evidence_kind == "greenhouse" and result.relationship_url == HOME
    assert calls == [HOME, GH]


def test_external_organization_claim_needs_backlink(monkeypatch):
    org = {"@type": "Organization", "name": "Acme", "url": HOME}
    patch_pages(monkeypatch, {"https://directory.example/": html(org), HOME: b"<p>Hello</p>"})
    assert not evidence.inspect("https://directory.example/", technology_roles())[0].evidence_kind


def test_five_page_limit(monkeypatch):
    pages = {HOME: b"".join(f'<a href="/jobs/{i}">Careers</a>'.encode() for i in range(20))}
    pages.update({HOME + f"jobs/{i}": b"No structured jobs" for i in range(20)})
    calls = patch_pages(monkeypatch, pages)
    evidence.inspect(HOME, technology_roles())
    assert len(calls) <= 5


@pytest.mark.parametrize(
    "changes",
    [
        {"relationship_url": "https://unrelated.example/"},
        {"observed_at": None},
        {"observed_at": timezone.now() - timedelta(days=8)},
        {"vacancy_url": "https://unrelated.example/job"},
        {"status": "rejected"},
        {"website": "https://127.0.0.1/"},
        {"vacancy_title": "Sales manager"},
        {"vacancy_location": "London, United Kingdom"},
        {"evidence_kind": ""},
        {"valid_until": timezone.now() - timedelta(seconds=1)},
    ],
)
def test_promotion_requires_evidence(campaign, changes):
    lead = lead_for(campaign)
    EmployerDiscoveryLead.objects.filter(pk=lead.pk).update(**changes)
    with pytest.raises(ValidationError):
        discovery.promote(lead.pk)
    assert not Company.objects.exists()


def test_daily_limits_shared_by_manual_runs(campaign, monkeypatch):
    campaign.daily_search_limit = 1
    campaign.save()
    monkeypatch.setattr(brave_search, "search", Mock(return_value=([], False)))
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    ready(run)
    discovery.step(run.pk)
    run.refresh_from_db()
    assert run.status == "complete" and run.diagnostic == "DailyBudgetExhausted"
    second = discovery.start(campaign.pk, manual=True)
    discovery.step(second.pk)
    assert brave_search.search.call_count == 1
    assert EmployerDiscoveryBudget.objects.get(campaign=campaign).searches == 1


def test_candidate_budget_carries_checkpoint_to_next_run(campaign, monkeypatch):
    campaign.daily_candidate_limit = 1
    campaign.save()
    monkeypatch.setattr(brave_search, "search", Mock(return_value=([JOB, HOME], False)))
    monkeypatch.setattr(discovery, "inspect", lambda *args: (evidence.Evidence(), "Unknown"))
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    ready(run)
    discovery.step(run.pk)
    ready(run)
    discovery.step(run.pk)
    run.refresh_from_db()
    assert run.status == "complete" and len(run.pending_leads) == 1
    next_run = discovery.start(campaign.pk, manual=True)
    assert next_run.pending_leads == run.pending_leads


def test_duplicate_delivery_and_expired_lease(campaign, monkeypatch):
    run = discovery.start(campaign.pk)
    claimed = discovery.claim(run.pk)
    assert claimed and discovery.claim(run.pk) is None
    old_token = claimed[2]
    EmployerDiscoveryRun.objects.filter(pk=run.pk).update(
        lease_until=timezone.now() - timedelta(seconds=1)
    )
    new_claim = discovery.claim(run.pk)
    assert new_claim[2] != old_token
    discovery.save_search(run.pk, old_token, campaign.queries[0], 0, [JOB], False)
    assert not EmployerDiscoveryLead.objects.filter(campaign=campaign).exists()


def test_disabling_campaign_fences_inflight_results(campaign):
    run = discovery.start(campaign.pk)
    token = discovery.claim(run.pk)[2]
    campaign.enabled = False
    campaign.save()
    discovery.save_search(run.pk, token, campaign.queries[0], 0, [JOB], False)
    assert not EmployerDiscoveryLead.objects.filter(campaign=campaign).exists()
    assert discovery.claim(run.pk) is None
    assert discovery.start(campaign.pk, manual=True) is None


def test_missing_credentials_no_network(campaign, settings, monkeypatch):
    settings.BRAVE_SEARCH_API_KEY = ""
    search = Mock()
    monkeypatch.setattr(brave_search, "search", search)
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    run.refresh_from_db()
    assert run.status == "failed" and "BRAVE_SEARCH_API_KEY" in run.diagnostic
    assert not EmployerDiscoveryBudget.objects.filter(campaign=campaign).exists()
    search.assert_not_called()


def test_transient_retries_are_bounded_and_charged(campaign, monkeypatch):
    monkeypatch.setattr(brave_search, "search", Mock(side_effect=HTTPStatusError(503)))
    run = discovery.start(campaign.pk)
    for _ in range(5):
        ready(run)
        discovery.step(run.pk)
    run.refresh_from_db()
    assert run.status == "failed" and run.failed == 1
    assert run.searched == 4
    assert EmployerDiscoveryBudget.objects.get(campaign=campaign).searches == 4


def test_rejection_survives_rediscovery(campaign, monkeypatch):
    lead = lead_for(campaign)
    lead.status = "rejected"
    lead.save()
    monkeypatch.setattr(brave_search, "search", Mock(return_value=([JOB], False)))
    # Use the same canonical key as the worker.
    import hashlib

    lead.key = hashlib.sha256(JOB.encode()).hexdigest()
    lead.save()
    discovery.step(lead.run_id)
    lead.refresh_from_db()
    assert lead.status == "rejected" and not Company.objects.exists()


def test_admin_permissions(campaign, user):
    model_admin = EmployerDiscoveryLeadAdmin(EmployerDiscoveryLead, admin.site)
    request = RequestFactory().post("/admin/")
    request.user = user
    queryset = EmployerDiscoveryLead.objects.filter(pk=lead_for(campaign).pk)
    with pytest.raises(PermissionDenied):
        model_admin.promote_selected(request, queryset)
    with pytest.raises(PermissionDenied):
        model_admin.reject_selected(request, queryset)
    user.user_permissions.add(Permission.objects.get(codename="change_employerdiscoverylead"))
    request.user = User.objects.get(pk=user.pk)
    assert not model_admin.has_promote_permission(request)
    assert "promote_selected" not in model_admin.get_actions(request)


def fake_brave(monkeypatch, response):
    original = httpx.Client
    requests = []

    def handle(request):
        requests.append(request)
        return response

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(brave_search.httpx, "Client", client)
    return requests


def test_brave_fixed_destination_and_headers(campaign, monkeypatch):
    requests = fake_brave(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "web": {"results": [{"url": JOB}]},
                "query": {"more_results_available": True},
            },
            headers={"X-RateLimit-Remaining": "1, 0", "X-RateLimit-Reset": "1, 3600"},
        ),
    )
    with collection_context():
        assert brave_search.search("public careers", 2) == ([JOB], True)
    assert str(requests[0].url).startswith(brave_search.ENDPOINT)
    assert requests[0].headers["X-Subscription-Token"] == "fake-brave-key"
    assert requests[0].url.params["offset"] == "2"
    assert CrawlHost.objects.get(host=brave_search.HOST).available_at > timezone.now() + timedelta(
        minutes=59
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"web": []}),
        httpx.Response(200, json={"web": {"results": [None]}}),
        httpx.Response(200, content=b"invalid json"),
        httpx.Response(302, headers={"Location": "https://attacker.example/"}),
        httpx.Response(200, content=b"x" * (brave_search.MAX_BYTES + 1)),
    ],
)
def test_brave_malformed_and_redirects(campaign, monkeypatch, response):
    requests = fake_brave(monkeypatch, response)
    with pytest.raises(ValueError), collection_context():
        brave_search.search("public careers", 0)
    assert len(requests) == 1


def test_brave_throttling(campaign, monkeypatch):
    fake_brave(monkeypatch, httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(HTTPStatusError), collection_context():
        brave_search.search("public careers", 0)
    assert CrawlHost.objects.get(host=brave_search.HOST).available_at > timezone.now() + timedelta(
        seconds=110
    )


def test_unsafe_and_blocked_candidates(campaign, monkeypatch):
    lead = lead_for(campaign)
    EmployerDiscoveryRun.objects.filter(pk=lead.run_id).update(pending_leads=[lead.pk])
    monkeypatch.setattr(discovery, "inspect", Mock(side_effect=UnsafeURL("sensitive payload")))
    discovery.step(lead.run_id)
    lead.refresh_from_db()
    assert lead.status == "unresolved" and lead.diagnostic == "UnsafeURL" and not lead.evidence_kind
    assert not Company.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_budget_reservations(campaign):
    assert connection.vendor == "postgresql"
    campaign.daily_search_limit = 1
    campaign.save()

    def attempt(_):
        close_old_connections()
        try:
            return discovery.reserve(
                EmployerDiscoveryCampaign.objects.get(pk=campaign.pk), "searches"
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt, range(4))) == 1
    assert EmployerDiscoveryBudget.objects.get(campaign=campaign).searches == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_promotion(campaign):
    lead = lead_for(campaign)

    def attempt(_):
        close_old_connections()
        try:
            return discovery.promote(lead.pk)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sum(pool.map(attempt, range(3))) == 1
    assert Company.objects.count() == Source.objects.count() == 1


def test_posted_today_is_accepted(monkeypatch):
    patch_pages(monkeypatch, {JOB: html(node(datePosted=timezone.now().date().isoformat()))})
    assert evidence.inspect(JOB, technology_roles())[0].evidence_kind == "jsonld"


def test_queries_rotate_and_paginate(campaign, monkeypatch):
    monkeypatch.setattr(brave_search, "search", Mock(return_value=([], True)))
    run = discovery.start(campaign.pk)
    for _ in range(3):
        ready(run)
        discovery.step(run.pk)
    assert [call.args for call in brave_search.search.call_args_list] == [
        (campaign.queries[0], 0),
        (campaign.queries[1], 0),
        (campaign.queries[0], 1),
    ]


def test_global_budget_caps_all_campaigns(campaign):
    second = EmployerDiscoveryCampaign.objects.create(
        name="Second",
        queries=campaign.queries,
        technology_roles=technology_roles(),
    )
    EmployerDiscoveryBudget.objects.create(
        campaign=campaign,
        day=timezone.now().date(),
        searches=30,
        candidates=100,
    )
    assert not discovery.reserve(second, "searches")
    assert not discovery.reserve(second, "candidates")


def test_dispatch_recovers_lost_and_interrupted_tasks(campaign, monkeypatch):
    from jobs import tasks

    publish = Mock()
    monkeypatch.setattr(tasks.discover_employers, "delay", publish)
    discovery.dispatch()
    run = EmployerDiscoveryRun.objects.get(campaign=campaign)
    publish.assert_called_once_with(run.pk)
    discovery.dispatch()
    assert publish.call_count == 1
    EmployerDiscoveryRun.objects.filter(pk=run.pk).update(
        next_dispatch_at=timezone.now() - timedelta(seconds=1),
        lease_until=timezone.now() - timedelta(seconds=1),
    )
    discovery.dispatch()
    assert publish.call_count == 2


def test_ashby_listing_evidence(monkeypatch):
    board = "https://jobs.ashbyhq.com/acme"
    api = "https://api.ashbyhq.com/posting-api/job-board/acme"
    job = board + "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    org = {"@type": "Organization", "name": "Acme", "url": HOME}
    patch_pages(
        monkeypatch,
        {
            HOME: html(org, f'<a href="{board}">Careers</a>'),
            api: json.dumps(
                {
                    "jobs": [
                        {
                            "id": "a",
                            "title": "Software Engineer",
                            "location": "Kyiv, Ukraine",
                            "jobUrl": job,
                            "isListed": True,
                        }
                    ]
                }
            ).encode(),
        },
    )
    result, _ = evidence.inspect(HOME, technology_roles())
    assert result.evidence_kind == "ashby"
    assert result.vacancy_url == job


def test_brave_timeout_is_retried(campaign, monkeypatch):
    monkeypatch.setattr(brave_search, "search", Mock(side_effect=httpx.ReadTimeout("private data")))
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    run.refresh_from_db()
    assert run.status == "active" and run.retries == 1 and run.diagnostic == "ReadTimeout"
    assert run.next_attempt_at > timezone.now()


def test_candidate_cooldown_preserves_checkpoint(campaign, monkeypatch):
    lead = lead_for(campaign)
    EmployerDiscoveryRun.objects.filter(pk=lead.run_id).update(pending_leads=[lead.pk])
    until = timezone.now() + timedelta(hours=2)
    monkeypatch.setattr(discovery, "inspect", Mock(side_effect=CrawlDeferred(until)))
    discovery.step(lead.run_id)
    run = EmployerDiscoveryRun.objects.get(pk=lead.run_id)
    assert run.pending_leads == [lead.pk] and run.next_attempt_at >= until
    assert EmployerDiscoveryBudget.objects.get(campaign=campaign).candidates == 1


@pytest.mark.parametrize(
    "location", ["Northern Ireland", "Remote EU timezone", "Remote excluding Germany"]
)
def test_ambiguous_or_excluded_locations(location):
    assert not evidence.location_matches(location)


def test_manual_attestation_supports_unknown_hosted_system(campaign):
    lead = lead_for(campaign)
    lead.career_url = "https://careers.vendor.example/acme"
    lead.vacancy_url = "https://careers.vendor.example/acme/jobs/1"
    lead.save()
    assert discovery.promote(lead.pk)
    source = Source.objects.get()
    assert source.adapter == "jsonld" and not source.enabled and source.discovery_pending


def test_workday_check_obeys_page_budget(monkeypatch):
    from jobs.services import network

    board = "https://acme.wd5.myworkdayjobs.com/External"
    base = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"
    org = {"@type": "Organization", "name": "Acme", "url": HOME}
    calls = patch_pages(
        monkeypatch,
        {
            HOME: html(org, f'<a href="{board}">Careers</a>'),
            **{
                base + f"/job/{i}": json.dumps(
                    {
                        "jobPostingInfo": {
                            "jobReqId": f"REQ{i}",
                            "title": "Software Engineer",
                            "location": "Germany",
                            "jobDescription": "Build software",
                        }
                    }
                ).encode()
                for i in range(10)
            },
        },
    )

    def request(url, **kwargs):
        calls.append(url)
        assert kwargs["payload"]["limit"] == 10
        return json.dumps(
            {"total": 10, "jobPostings": [{"externalPath": f"/job/{i}"} for i in range(10)]}
        ).encode(), url

    monkeypatch.setattr(network, "request_public_document", request)
    result, _ = evidence.inspect(HOME, technology_roles())
    assert result.evidence_kind == "workday"
    assert len(calls) == 5


def login_staff(client, user):
    from django_otp import DEVICE_ID_SESSION_KEY

    user.is_staff = True
    user.save()
    device = user.totpdevice_set.create(name="discovery-test", confirmed=True)
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()


def test_admin_renders_escaped_evidence_and_protects_mutations(campaign, client, user):
    from django.test import Client

    lead = lead_for(campaign)
    lead.name = '<script>alert("untrusted")</script>'
    lead.save()
    user.is_superuser = True
    user.save()
    login_staff(client, user)
    for route in ("employerdiscoverycampaign", "employerdiscoveryrun", "employerdiscoverylead"):
        assert client.get(f"/admin/jobs/{route}/").status_code == 200
    url = f"/admin/jobs/employerdiscoverylead/{lead.pk}/change/"
    response = client.get(url)
    assert response.status_code == 200
    assert b"&lt;script&gt;" in response.content
    assert b'<script>alert("untrusted")</script>' not in response.content
    # GET cannot trigger an admin action.
    client.get(
        "/admin/jobs/employerdiscoverylead/",
        {
            "action": "promote_selected",
            "_selected_action": [lead.pk],
        },
    )
    assert not Company.objects.exists()
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.cookies = client.cookies
    assert (
        csrf_client.post(
            "/admin/jobs/employerdiscoverylead/",
            {
                "action": "promote_selected",
                "_selected_action": [lead.pk],
            },
        ).status_code
        == 403
    )


def test_staff_without_model_permission_cannot_access_leads(campaign, client, user):
    lead_for(campaign)
    login_staff(client, user)
    assert client.get("/admin/jobs/employerdiscoverylead/").status_code == 403


def test_admin_stale_edit_cannot_undo_rejection(campaign, user):
    lead = lead_for(campaign)
    stale = EmployerDiscoveryLead.objects.get(pk=lead.pk)
    EmployerDiscoveryLead.objects.filter(pk=lead.pk).update(status="rejected")
    request = RequestFactory().post("/admin/")
    request.user = user
    model_admin = EmployerDiscoveryLeadAdmin(EmployerDiscoveryLead, admin.site)
    model_admin.save_model(request, stale, Mock(), True)
    lead.refresh_from_db()
    assert lead.status == "rejected"


@pytest.mark.django_db(transaction=True)
def test_concurrent_task_claims(campaign):
    run = discovery.start(campaign.pk)

    def attempt(_):
        close_old_connections()
        try:
            return discovery.claim(run.pk) is not None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt, range(4))) == 1


def test_invalid_campaign_fails_visibly_without_network(campaign, monkeypatch):
    campaign.queries = [None]
    campaign.save()
    search = Mock()
    monkeypatch.setattr(brave_search, "search", search)
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    run.refresh_from_db()
    assert run.status == "failed" and run.diagnostic == "InvalidCampaignConfiguration"
    search.assert_not_called()


def test_excessive_retry_window_fails_without_stranding_lease(campaign, monkeypatch):
    monkeypatch.setattr(
        brave_search, "search", Mock(side_effect=HTTPStatusError(503, "9999999999"))
    )
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    run.refresh_from_db()
    assert run.status == "failed" and run.diagnostic == "RetryWindowTooLong"
    assert run.lease_until is None
