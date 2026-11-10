import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobs.services.ats import provider_identity
from jobs.services.bamboohr import collect as collect_bamboohr
from jobs.services.inventories import collect
from jobs.services.sources import ADAPTERS, normalize

FIXTURES = Path(__file__).parent / "fixtures" / "public_careers"
BOARDS = [
    ("protean-board", "proteantecs/D5.00E", 16),
    ("alice-current", "alice/D5.005", 42),
    ("exco-board", "exco/F0.005", 7),
    ("g2-board", "g2risksolutions/C9.003", 6),
    ("firebolt-board", "firebolt/26.004", 3),
    ("faye-board", "withfaye/87.00A", 15),
]


def config(**changes):
    return SimpleNamespace(
        **{
            "adapter": "comeet",
            "url": "https://www.comeet.com/jobs/proteantecs/D5.00E",
            "max_items": 100000,
            "max_response_bytes": 2 * 1024 * 1024,
            **changes,
        }
    )


def page(rows):
    return ("<script>COMPANY_POSITIONS_DATA=" + json.dumps(rows) + ";</script>").encode()


@pytest.mark.parametrize("name,board,count", BOARDS)
def test_hosted_company_inventories(name, board, count):
    rows = json.loads((FIXTURES / f"{name}.json").read_text())
    url = f"https://www.comeet.com/jobs/{board}"
    result, _ = collect(config(url=url), {}, fetch=lambda url: page(rows))
    assert len(result.postings) == count
    assert not result.complete and result.expected_total is None
    assert len({p.provider_key for p in result.postings}) == count
    for posting in result.postings:
        assert normalize(posting)["description"]
        assert provider_identity(posting.url) == posting.provider_key


@pytest.mark.parametrize("change", ["foreign", "uid", "location", "internal", "over_budget"])
def test_generic_hosted_boards_fail_closed(change):
    rows = json.loads((FIXTURES / "protean-board.json").read_text())
    source = config()
    if change == "foreign":
        rows[0]["url_comeet_hosted_page"] = rows[0]["url_comeet_hosted_page"].replace(
            "proteantecs", "other"
        )
    elif change == "uid":
        rows[0]["uid"] = "XX.000"
    elif change == "location":
        rows[0]["location"] = []
    elif change == "internal":
        rows[0]["is_internal"] = True
    else:
        source.max_items = 1
    with pytest.raises(ValueError):
        collect(source, {}, fetch=lambda url: page(rows))


def bamboo_source(**changes):
    return config(adapter="bamboohr", url="https://alviere.bamboohr.com/careers/list", **changes)


def bamboo_fetch(url, **kwargs):
    name = "bamboo" if url.endswith("/list") else "bamboo-detail"
    return (FIXTURES / f"{name}.json").read_bytes()


def test_bamboohr_normalizes_public_detail():
    inventory = json.loads(bamboo_fetch("/list"))
    inventory["result"] = inventory["result"][:1]
    inventory["meta"]["totalCount"] = 1

    def fetch(url, **kwargs):
        return json.dumps(inventory).encode() if url.endswith("/list") else bamboo_fetch(url)

    result = collect_bamboohr(bamboo_source(), {}, fetch=fetch)
    posting = result.postings[0]
    assert posting.title == "Backend Software Engineer"
    assert posting.location == "Lisbon, Portugal"
    assert posting.provider_key == "bamboohr:alviere.bamboohr.com:36"
    assert "Company Summary:" in posting.description
    assert not result.complete


@pytest.mark.parametrize("change", ["foreign", "closed", "description", "total", "duplicate"])
def test_bamboohr_rejects_changed_or_malformed_data(change):
    inventory = json.loads(bamboo_fetch("/list"))
    detail = json.loads(bamboo_fetch("/detail"))
    if change == "total":
        inventory["meta"]["totalCount"] = 100
    elif change == "duplicate":
        inventory["result"][1] = copy.deepcopy(inventory["result"][0])
    else:
        job = detail["result"]["jobOpening"]
        if change == "foreign":
            job["jobOpeningShareUrl"] = "https://other.bamboohr.com/careers/36"
        elif change == "closed":
            job["jobOpeningStatus"] = "Closed"
        else:
            job["description"] = ""

    def fetch(url, **kwargs):
        return json.dumps(inventory if url.endswith("/list") else detail).encode()

    with pytest.raises(ValueError):
        collect_bamboohr(bamboo_source(), {}, fetch=fetch)


@pytest.mark.parametrize("cursor", [{"offset": -1}, {"offset": True}, {"payload_hash": "old"}])
def test_bamboohr_rejects_invalid_checkpoint(cursor):
    with pytest.raises(ValueError):
        collect_bamboohr(bamboo_source(), cursor, fetch=bamboo_fetch)


def test_foundational_native_job_metadata():
    adapter = ADAPTERS["foundational"]
    listings = adapter.discover(
        (FIXTURES / "foundational.html").read_bytes(),
        "https://www.foundational.io/careers",
        100,
    )
    assert len(listings) == 1
    listing = adapter.metadata(listings[0], (FIXTURES / "foundational-detail.html").read_bytes())
    assert listing.title == "Backend Engineer"
    assert listing.location == "Tel Aviv"
    assert "Python" in listing.description
    assert "Please fill" not in listing.description


def test_foundational_rejects_external_links_and_changed_markup():
    adapter = ADAPTERS["foundational"]
    with pytest.raises(ValueError):
        adapter.discover(
            b'<a href="https://evil.example/careers/backend-engineer">Engineer</a>',
            "https://www.foundational.io/careers",
            100,
        )


@pytest.mark.django_db
def test_configuration_preview_apply_and_idempotency(capsys):
    from django.core.management import call_command

    from jobs.models import Company, Source

    company = Company.objects.create(name="Alviere", website="https://alviere.com")
    source = Source.objects.create(
        name="Alviere careers",
        kind="company",
        company=company,
        url="https://alviere.com/careers/",
        needs_adapter=True,
        adapter="jsonld",
    )
    call_command("configure_career_adapters")
    source.refresh_from_db()
    assert source.needs_adapter and source.adapter == "jsonld"
    call_command("configure_career_adapters", apply=True)
    source.refresh_from_db()
    assert source.url == "https://alviere.bamboohr.com/careers/list"
    assert source.adapter == "bamboohr" and not source.needs_adapter
    assert not source.enabled and not source.authoritative and not source.closure_enabled
    call_command("configure_career_adapters", apply=True)
    assert "Updated 0 disabled sources" in capsys.readouterr().out


@pytest.mark.django_db
def test_configuration_rolls_back_on_conflicting_endpoint():
    from django.core.management import call_command
    from django.core.management.base import CommandError

    from jobs.models import Source

    original = Source.objects.create(
        name="Alviere", url="https://alviere.com/careers/", needs_adapter=True
    )
    Source.objects.create(
        name="Already configured",
        url="https://alviere.bamboohr.com/careers/list",
        adapter="bamboohr",
    )
    with pytest.raises(CommandError, match="already configured"):
        call_command("configure_career_adapters", apply=True)
    original.refresh_from_db()
    assert original.needs_adapter


def test_4m_ashby_inventory():
    source = config(adapter="ashby", url="https://api.ashbyhq.com/posting-api/job-board/4M")
    result, _ = collect(source, {}, fetch=lambda url: (FIXTURES / "4m-ashby.json").read_bytes())
    assert len(result.postings) == 6
    assert result.complete and result.expected_total == 6
    assert all(p.provider_key.startswith("ashby:4M:") for p in result.postings)
    assert all(normalize(p)["description"] for p in result.postings)


def test_bamboohr_resumes_bounded_detail_requests():
    rows = [{"id": str(uid), "jobOpeningName": "Engineer"} for uid in range(1, 13)]
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith("/list"):
            return json.dumps({"meta": {"totalCount": 12}, "result": rows}).encode()
        detail = json.loads(bamboo_fetch("/detail"))
        detail["result"]["jobOpening"]["jobOpeningShareUrl"] = url.removesuffix("/detail")
        return json.dumps(detail).encode()

    source = bamboo_source()
    first = collect_bamboohr(source, {}, fetch=fetch)
    assert len(first.postings) == 10 and first.cursor["offset"] == 10
    assert len(calls) == 11
    second = collect_bamboohr(source, first.cursor, fetch=fetch)
    assert len(second.postings) == 2 and not second.cursor
    assert len({p.provider_key for p in first.postings + second.postings}) == 12
    assert not first.complete and not second.complete


@pytest.mark.parametrize(
    "url",
    [
        "https://alviere.bamboohr.com.evil.example/careers/list",
        "https://127.0.0.1/careers/list",
        "https://alviere.bamboohr.com/careers/list?token=untrusted",
    ],
)
def test_bamboohr_rejects_unexpected_endpoint_before_fetch(url):
    source = bamboo_source()
    source.url = url

    def fetch(*args, **kwargs):
        pytest.fail("Unexpected endpoint was fetched")

    with pytest.raises(ValueError):
        collect_bamboohr(source, {}, fetch=fetch)


@pytest.mark.django_db
def test_bamboohr_ingestion_keeps_company_and_never_closes(monkeypatch, settings):
    from jobs.models import Company, Job, Source
    from jobs.services import ingestion

    settings.JOB_AI_NORMALIZATION_ENABLED = False
    settings.JOB_NORMALIZATION_BATCH_ENABLED = False
    company = Company.objects.create(name="Alviere", website="https://alviere.com")
    source = Source.objects.create(
        name="Alviere careers",
        kind="company",
        company=company,
        employer_name="Alviere",
        adapter="bamboohr",
        url="https://alviere.bamboohr.com/careers/list",
        enabled=True,
    )

    def fetch(url, **kwargs):
        if url.endswith("/51/detail"):
            return (FIXTURES / "bamboo-detail-51.json").read_bytes()
        return bamboo_fetch(url, **kwargs)

    monkeypatch.setattr(ingestion, "fetch_public", fetch)
    ingestion.ingest(source.pk)
    assert Job.objects.filter(company_profile=company).count() == 2
    assert not Job.objects.filter(employer_source__isnull=False).exists()
    assert set(Job.objects.values_list("company", flat=True)) == {"Alviere"}
    run = source.ingestionrun_set.get()
    assert run.status == "success" and run.imported == 2 and not run.complete
    source.refresh_from_db()
    assert source.last_complete_at is None
