import importlib
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.apps import apps
from django.db import connection
from django.utils import timezone

from jobs.models import (
    Company,
    EmployerDiscoveryBudget,
    EmployerDiscoveryCampaign,
    EmployerDiscoveryLead,
    EmployerDiscoveryRun,
    Job,
    Source,
)
from jobs.services import employer_discovery as discovery
from jobs.services import employer_evidence, pituchim
from jobs.services.crawl import collection_context
from jobs.services.employer_defaults import technology_roles
from jobs.services.network import HTTPStatusError

pytestmark = pytest.mark.django_db
HOME = "https://acme.example/"


def directory(cards=None, *, page=1, total=2, next_url="/startups?page=2"):
    cards = cards if cards is not None else [("Acme", HOME, True)]
    body = "".join(
        f'<article class="company-row"><div class="company-row-heading">'
        f'<h3><a href="{website}">{name}</a></h3></div>'
        f"{'<span class=company-hiring>Hiring</span>' if hiring else ''}"
        '<a href="https://tracker.example/">Visit site</a></article>'
        for name, website, hiring in cards
    )
    return (
        body
        + f"<p>Page {page} of {total}</p>"
        + (f'<a href="{next_url}">Next</a>' if next_url else "")
    ).encode()


@pytest.fixture
def campaign(settings, monkeypatch):
    settings.BRAVE_SEARCH_API_KEY = ""
    monkeypatch.setattr(pituchim, "check_robots", Mock())
    return EmployerDiscoveryCampaign.objects.create(
        name="Pituchim test",
        provider="pituchim",
        enabled=True,
        technology_roles=technology_roles(),
    )


def ready(run):
    EmployerDiscoveryRun.objects.filter(pk=run.pk).update(next_attempt_at=timezone.now())


def test_seed_disabled_without_key():
    migration = importlib.import_module("jobs.migrations.0051_seed_pituchim_discovery")
    migration.seed(apps, Mock(connection=connection))
    migration.seed(apps, Mock(connection=connection))
    campaign = EmployerDiscoveryCampaign.objects.get(name="Pituchim hiring companies")
    campaign.full_clean()
    assert not campaign.enabled and campaign.provider == "pituchim" and not campaign.queries


def test_hiring_filter_extracts_only_company_headers():
    data = directory(
        [
            ("Acme", HOME, True),
            ("Not hiring", "https://other.example/", False),
            ("Acme", HOME, True),
            ("Unsafe", "http://localhost/", True),
            ("Directory", "https://pituchim.com/", True),
        ]
    )
    companies, more = pituchim.parse(data, 1)
    assert companies == [pituchim.DirectoryCompany("Acme", HOME)] and more


def test_page_without_hiring_is_valid():
    data = directory([("Acme", HOME, False)])
    assert pituchim.parse(data, 1) == ([], True)
    assert pituchim.parse(data, 1, hiring_only=False)[0] == [
        pituchim.DirectoryCompany("Acme", HOME)
    ]


@pytest.mark.parametrize(
    "data",
    [
        b"<h1>Challenge</h1>",
        directory(next_url="https://attacker.example/startups?page=2"),
        directory(next_url="/api/companies?page=2"),
        directory(next_url="/startups?page=3"),
        directory(next_url=None),
        directory(page=2),
        directory(cards=[("Bad", "http://localhost", True)]),
        directory(cards=[("Acme", HOME, True)] * 61),
    ],
)
def test_malformed_markup_and_pagination_fail_closed(data):
    with pytest.raises(ValueError):
        pituchim.parse(data, 1)


def test_final_page():
    assert pituchim.parse(directory(page=3, total=3, next_url=None), 3)[1] is False


def test_fetch_respects_robots_and_uses_only_html(monkeypatch):
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return (
            (
                b"User-agent: *\nAllow: /\nDisallow: /api/\n"
                if url.endswith("robots.txt")
                else directory()
            ),
            url,
        )

    monkeypatch.setattr(pituchim, "fetch_public_document", fetch)
    with collection_context():
        companies, _ = pituchim.fetch(1)
    assert companies and calls == ["https://pituchim.com/robots.txt", pituchim.BASE]


def test_disallowed_robots_blocks_directory(monkeypatch):
    fetch = Mock(
        return_value=(b"User-agent: *\nDisallow: /startups", "https://pituchim.com/robots.txt")
    )
    monkeypatch.setattr(pituchim, "fetch_public_document", fetch)
    with pytest.raises(ValueError):
        pituchim.fetch(1)
    assert fetch.call_count == 1


@pytest.mark.parametrize("status", [403, 429, 503])
def test_robots_failure_does_not_bypass_rules(monkeypatch, status):
    fetch = Mock(side_effect=HTTPStatusError(status))
    monkeypatch.setattr(pituchim, "fetch_public_document", fetch)
    with pytest.raises(HTTPStatusError):
        pituchim.fetch(1)
    assert fetch.call_count == 1


def test_worker_needs_no_api_key_and_keeps_identity_hints(campaign, monkeypatch):
    monkeypatch.setattr(
        pituchim, "fetch", Mock(return_value=([pituchim.DirectoryCompany("Acme", HOME)], False))
    )
    monkeypatch.setattr(
        discovery.brave_search, "search", Mock(side_effect=AssertionError("No Brave calls"))
    )
    inspect = Mock(return_value=(employer_evidence.Evidence(), "UncertainHiring"))
    monkeypatch.setattr(discovery, "inspect", inspect)
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    ready(run)
    discovery.step(run.pk)
    lead = EmployerDiscoveryLead.objects.get(campaign=campaign)
    assert lead.name == "Acme" and lead.website == HOME and lead.search_query == pituchim.BASE
    assert not Company.objects.exists() and not Job.objects.exists()
    ready(run)
    discovery.step(run.pk)
    lead.refresh_from_db()
    assert lead.status == "unresolved" and lead.name == "Acme" and lead.website == HOME
    inspect.assert_called_once_with(
        HOME, technology_roles(), company_hint="Acme", website_hint=HOME
    )
    ready(run)
    discovery.step(run.pk)
    run.refresh_from_db()
    assert run.status == "complete" and run.diagnostic == "DirectoryCycleComplete"
    assert run.searched == 2 and run.inspected == 1
    assert EmployerDiscoveryBudget.objects.get(campaign=campaign).searches == 2
    campaign.refresh_from_db()
    assert campaign.directory_page == 1
    discovery.brave_search.search.assert_not_called()


def test_pagination_checkpoint_and_idempotence(campaign, monkeypatch):
    monkeypatch.setattr(pituchim, "fetch", Mock(return_value=([], True)))
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    ready(run)
    discovery.step(run.pk)
    campaign.refresh_from_db()
    assert campaign.directory_page == 2
    ready(run)
    discovery.step(run.pk)
    ready(run)
    discovery.step(run.pk)
    assert [call.args for call in pituchim.fetch.call_args_list] == [(1,), (2,)]


def test_each_request_reserves_budget_before_io(campaign, monkeypatch):
    campaign.daily_search_limit = 1
    campaign.save()
    fetch = Mock()
    monkeypatch.setattr(pituchim, "fetch", fetch)
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    ready(run)
    discovery.step(run.pk)
    run.refresh_from_db()
    assert run.status == "complete" and run.diagnostic == "DailyBudgetExhausted"
    fetch.assert_not_called()
    assert EmployerDiscoveryBudget.objects.get(campaign=campaign).searches == 1


def test_directory_hints_allow_official_linked_board_without_org_schema(monkeypatch):
    import json

    board = "https://job-boards.greenhouse.io/acme"
    api = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    pages = {
        HOME: f'<a href="{board}">Careers</a>'.encode(),
        api: json.dumps(
            {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Software Engineer",
                        "location": {"name": "Israel"},
                        "absolute_url": board + "/jobs/1",
                    }
                ]
            }
        ).encode(),
    }
    monkeypatch.setattr(
        employer_evidence, "fetch_public_document", lambda url, **kw: (pages[url], url)
    )
    result, _ = employer_evidence.inspect(
        HOME, technology_roles(), company_hint="Acme", website_hint=HOME
    )
    assert result.evidence_kind == "greenhouse" and result.relationship_url == HOME
    assert not Company.objects.exists() and not Source.objects.exists()


def test_rejected_directory_leads_stay_rejected(campaign, monkeypatch):
    monkeypatch.setattr(
        pituchim, "fetch", Mock(return_value=([pituchim.DirectoryCompany("Acme", HOME)], True))
    )
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    ready(run)
    discovery.step(run.pk)
    lead = EmployerDiscoveryLead.objects.get(campaign=campaign)
    lead.status = "rejected"
    lead.save()
    # The queued check sees the rejection before persisting any new evidence.
    monkeypatch.setattr(discovery, "inspect", Mock(return_value=(employer_evidence.Evidence(), "")))
    ready(run)
    discovery.step(run.pk)
    lead.refresh_from_db()
    assert lead.status == "rejected"


def test_page_result_fenced_after_disable(campaign):
    run = discovery.start(campaign.pk)
    token = discovery.claim(run.pk)[2]
    campaign.enabled = False
    campaign.save()
    discovery.save_directory(run.pk, token, 1, [pituchim.DirectoryCompany("Acme", HOME)], True)
    assert not EmployerDiscoveryLead.objects.filter(campaign=campaign).exists()


def test_directory_rules_survive_page_cooldown(campaign, monkeypatch):
    from jobs.services.crawl import CrawlDeferred

    monkeypatch.setattr(
        pituchim, "fetch", Mock(side_effect=CrawlDeferred(timezone.now() + timedelta(minutes=2)))
    )
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    ready(run)
    discovery.step(run.pk)
    run.refresh_from_db()
    assert run.retries == 1 and run.status == "active"
    assert pituchim.check_robots.call_count == 1
    monkeypatch.setattr(pituchim, "fetch", Mock(return_value=([], True)))
    ready(run)
    discovery.step(run.pk)
    campaign.refresh_from_db()
    assert campaign.directory_page == 2
    assert pituchim.check_robots.call_count == 1
    assert EmployerDiscoveryBudget.objects.get(campaign=campaign).searches == 3


def test_directory_rules_expire_before_another_page_request(campaign, monkeypatch):
    run = discovery.start(campaign.pk)
    discovery.step(run.pk)
    campaign.refresh_from_db()
    campaign.query_pages["pituchim_robots_until"] = (
        timezone.now() - timedelta(seconds=1)
    ).timestamp()
    campaign.save()
    ready(run)
    discovery.step(run.pk)
    assert pituchim.check_robots.call_count == 2


def test_hiring_evidence_promotes_pending_company_only(campaign, monkeypatch):
    monkeypatch.setattr(
        pituchim, "fetch", Mock(return_value=([pituchim.DirectoryCompany("Acme", HOME)], False))
    )
    observed = employer_evidence.Evidence(
        name="Acme",
        website=HOME,
        career_url=HOME + "careers",
        vacancy_title="Software Engineer",
        vacancy_location="Israel",
        vacancy_url=HOME + "careers/1",
        relationship_url=HOME,
        observed_at=timezone.now(),
        evidence_kind="jsonld",
    )
    monkeypatch.setattr(discovery, "inspect", Mock(return_value=(observed, "")))
    run = discovery.start(campaign.pk)
    for _ in range(3):
        ready(run)
        discovery.step(run.pk)
    lead = EmployerDiscoveryLead.objects.get(campaign=campaign)
    assert lead.status == "confirmed" and lead.company_id
    assert not lead.company.website_verified and lead.company.review_required
    source = Source.objects.get(company=lead.company)
    assert not source.enabled and source.discovery_pending and not source.authoritative
    assert not Job.objects.exists()
