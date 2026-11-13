import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections
from django.test import RequestFactory
from django.utils import timezone

from jobs import tasks
from jobs.models import Company, CompanyAlias, CrawlHost, CrawlLease, Job, Source
from jobs.services import catalogue, ingestion, inventories
from jobs.services.crawl import CrawlDeferred, acquire, collection_context
from jobs.services.inventories import CollectionResult, WorkdayAdapter
from jobs.services.network import HTTPStatusError
from jobs.services.sources import Listing

pytestmark = pytest.mark.django_db
GH = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
WD = "https://acme.wd5.myworkdayjobs.com/External"


def employer(name="Acme", **kwargs):
    kwargs.setdefault(
        "company",
        Company.objects.create(name=name, website="https://acme.example/", website_verified=True),
    )
    return Source.objects.create(
        name=name,
        kind=Source.Kind.COMPANY,
        adapter="greenhouse",
        url=GH + ("&endpoint=" + name.replace(" ", "_") if name != "Acme" else ""),
        employer_name="Acme",
        enabled=True,
        authoritative=True,
        closure_enabled=True,
        **kwargs,
    )


def listing(pk=1, **kwargs):
    return Listing(
        url=f"https://job-boards.greenhouse.io/acme/jobs/{pk}",
        title="Engineer",
        provider_key=f"greenhouse:acme:{pk}",
        **kwargs,
    )


def run(source, monkeypatch, postings, *, complete=True, cursor=None, expected=None):
    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
    result = CollectionResult(
        postings, "greenhouse:acme:", complete, cursor or {}, expected_total=expected
    )
    monkeypatch.setattr(ingestion, "collect", lambda *a, **kw: (result, b""))
    ingestion.ingest(source.pk)
    source.refresh_from_db()
    return source.ingestionrun_set.latest("pk")


def test_csv_idempotence_and_no_implicit_trust():
    csv = "name,website,career_url\nAcme,https://www.acme.example,https://acme.wd5.myworkdayjobs.com/External\n"
    assert catalogue.import_companies(StringIO(csv)) == (1, 0)
    assert catalogue.import_companies(StringIO(csv)) == (0, 1)
    company = Company.objects.get()
    source = company.career_sources.get()
    assert not company.website_verified and company.official_domain is None
    assert source.discovery_pending and not source.enabled
    assert source.adapter == "workday"
    with pytest.raises(ValueError):
        catalogue.candidate(company, WD, evidence=company.website, trusted=True)
    with pytest.raises(ValueError):
        catalogue.import_companies(
            StringIO("name,website\nNew,https://new.example\nBad,http://localhost\n")
        )
    assert Company.objects.count() == 1


def test_verified_merge_preserves_references_aliases_and_ambiguous_names(application):
    first = Company.objects.create(name="Acme", website="https://acme.example")
    second = Company.objects.create(name="Acme", website="https://www.acme.example/corporate")
    ambiguous = Company.objects.create(name="Acme", website="https://other.example")
    old_key = second.identity_key
    source = employer(company=second)
    application.job.company_profile = second
    application.job.save()
    catalogue.verify_company(first)
    merged = catalogue.verify_company(second)
    application.job.refresh_from_db()
    source.refresh_from_db()
    assert merged.pk == first.pk == application.job.company_profile_id == source.company_id
    assert CompanyAlias.objects.get(legacy_identity=old_key).company_id == first.pk
    assert Company.objects.filter(pk=ambiguous.pk, website_verified=False).exists()
    assert application.job.application_set.get().pk == application.pk


def test_discovery_evidence_and_unsupported_review(monkeypatch):
    company = catalogue.verify_company(
        Company.objects.create(name="Acme", website="https://acme.example")
    )
    pages = {
        "https://acme.example/": b'<a href="/careers">Careers</a>',
        "https://acme.example/careers": (
            b'<a href="https://acme.wd5.myworkdayjobs.com/en-US/External">Open roles</a>'
        ),
    }
    monkeypatch.setattr(catalogue, "fetch_public_document", lambda url: (pages[url], url))
    catalogue.discover_company(company)
    supported = company.career_sources.get(adapter="workday")
    unsupported = company.career_sources.get(adapter="jsonld")
    assert supported.enabled and supported.authoritative and not supported.closure_enabled
    assert supported.discovery_url == "https://acme.example/careers"
    assert unsupported.discovery_pending and not unsupported.enabled
    # Another company's link cannot hijack a shared endpoint's ownership.
    other = catalogue.verify_company(
        Company.objects.create(name="Other", website="https://other.example")
    )
    catalogue.candidate(other, WD, evidence=other.website, trusted=True)
    supported.refresh_from_db()
    other.refresh_from_db()
    assert supported.company_id == company.pk and other.review_required
    supported.enabled = False
    supported.review_reason = "Operator disabled automatic activation"
    supported.save()
    catalogue.discover_company(company)
    supported.refresh_from_db()
    assert not supported.enabled


def test_foreign_redirect_requires_review(monkeypatch):
    company = catalogue.verify_company(
        Company.objects.create(name="Acme", website="https://acme.example")
    )
    monkeypatch.setattr(
        catalogue, "fetch_public_document", lambda url: (b"", "https://other.example")
    )
    with pytest.raises(ValueError, match="different domain"):
        catalogue.discover_company(company)
    assert not Source.objects.exists()


def test_lifecycle_two_checks_partial_failure_reopen_and_shadow(monkeypatch):
    source = employer()
    run(source, monkeypatch, [listing()])
    job = Job.objects.get()
    assert job.active and job.lifecycle == "open"
    run(source, monkeypatch, [])
    assert job.origins.get().missing_count == 1
    run(source, monkeypatch, [], complete=False)
    assert job.origins.get().missing_count == 1
    run(source, monkeypatch, [])
    job.refresh_from_db()
    assert not job.active and job.lifecycle == "closed"
    assert job.status_reason == "employer_absent"
    run(source, monkeypatch, [listing()])
    job.refresh_from_db()
    assert job.active and job.lifecycle == "open" and job.origins.get().missing_count == 0
    assert list(job.lifecycle_events.values_list("status", flat=True)) == ["closed", "open"]
    source.closure_enabled = False
    source.save()
    run(source, monkeypatch, [])
    shadow = run(source, monkeypatch, [])
    job.refresh_from_db()
    assert job.active and shadow.would_close == 1


def test_overlapping_endpoints_overrides_and_employer_filled(monkeypatch):
    first = employer()
    second = employer("Acme alternate")
    run(first, monkeypatch, [listing()])
    run(second, monkeypatch, [listing()])
    run(first, monkeypatch, [])
    run(first, monkeypatch, [])
    job = Job.objects.get()
    assert job.active
    job.content_overrides = {"title": "Curated title"}
    job.status_override = "archived"
    job.save()
    run(second, monkeypatch, [listing(employer_status="filled")])
    job.refresh_from_db()
    assert job.lifecycle == "filled" and not job.active and job.title == "Curated title"
    assert job.imported_content["title"] == "Engineer"
    run(second, monkeypatch, [listing()])
    job.refresh_from_db()
    assert job.lifecycle == "open" and not job.active
    job.status_override = ""
    job.save()
    assert job.active


def test_resumed_inventory_only_final_page_establishes_absence(monkeypatch):
    source = employer()
    run(source, monkeypatch, [listing(99)])
    run(source, monkeypatch, [listing(1)], complete=False, cursor={"offset": 1}, expected=2)
    token = source.inventory_token
    assert (
        token
        and Job.objects.get(provider_key="greenhouse:acme:99").origins.get().missing_count == 0
    )
    completed = run(source, monkeypatch, [listing(2)], expected=2)
    assert completed.complete and source.inventory_token is None
    assert Job.objects.get(provider_key="greenhouse:acme:99").origins.get().missing_count == 1
    # An overlapping/replayed page cannot claim an inventory with missing distinct identities.
    run(source, monkeypatch, [listing(1)], complete=False, cursor={"offset": 1}, expected=2)
    bad = run(source, monkeypatch, [listing(1)], expected=2)
    assert not bad.complete
    assert Job.objects.get(provider_key="greenhouse:acme:99").origins.get().missing_count == 1


def test_failure_and_429_preserve_only_safe_checkpoints(monkeypatch):
    source = employer()
    run(source, monkeypatch, [listing()], complete=False, cursor={"offset": 10})
    token = source.inventory_token
    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())

    def limited(*args, **kwargs):
        raise HTTPStatusError(429, "3600")

    monkeypatch.setattr(ingestion, "collect", limited)
    ingestion.ingest(source.pk)
    source.refresh_from_db()
    assert source.inventory_token == token and source.inventory_cursor == {"offset": 10}
    assert source.next_run_at > timezone.now() + timedelta(minutes=59)
    assert source.lease_token is None and source.last_complete_at is None
    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
    monkeypatch.setattr(ingestion, "collect", Mock(side_effect=ValueError("malformed")))
    ingestion.ingest(source.pk)
    source.refresh_from_db()
    assert source.inventory_token is None and not source.inventory_cursor
    assert Job.objects.get().active


def workday_request(total=11, *, malformed=False, duplicate=False):
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/jobs"):
            offset = kwargs["payload"]["offset"]
            return json.dumps(
                {
                    "total": total,
                    "jobPostings": [
                        {"externalPath": f"/job/Remote/Engineer_JR{i}"}
                        for i in range(offset, min(offset + 10, total))
                    ],
                }
            ).encode(), url
        req = url.rsplit("_", 1)[-1]
        info = {
            "title": "Engineer",
            "jobReqId": "same" if duplicate else req,
            "location": "Remote",
            "jobDescription": "<h2>Requirements</h2><p>Python</p>",
        }
        return json.dumps({"jobPostingInfo": {} if malformed else info}).encode(), url

    return request, calls


def test_workday_pagination_empty_malformed_and_drift():
    source = Source(url=WD, max_response_bytes=1000000)
    request, calls = workday_request()
    first = WorkdayAdapter().collect(source, {}, request=request)
    assert not first.complete and first.cursor == {"offset": 10, "total": 11}
    assert len(first.postings) == 10 and len(calls) == 11
    assert first.postings[0].provider_key == "workday:acme:JR0"
    last = WorkdayAdapter().collect(source, first.cursor, request=request)
    assert last.complete and not last.cursor and len(last.postings) == 1
    request, _ = workday_request(0)
    assert WorkdayAdapter().collect(source, {}, request=request).complete
    request, _ = workday_request(malformed=True)
    with pytest.raises(ValueError, match="identity"):
        WorkdayAdapter().collect(source, {}, request=request)
    request, _ = workday_request(12)
    with pytest.raises(ValueError, match="changed"):
        WorkdayAdapter().collect(source, first.cursor, request=request)
    for url in [
        WD + "?location=Israel",
        "https://acme.wd5.myworkdayjobs.com.evil.example/External",
    ]:
        with pytest.raises(ValueError):
            inventories.workday_site(url)


def test_more_than_500_postings(monkeypatch):
    source = employer(max_items=1000)
    payload = {
        "jobs": [
            {
                "id": i,
                "title": "Engineer",
                "content": "Python",
                "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{i}",
            }
            for i in range(501)
        ],
        "meta": {"total": 501},
    }
    monkeypatch.setattr(ingestion, "fetch_public", lambda url: json.dumps(payload).encode())
    for _ in range(3):
        Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
        ingestion.ingest(source.pk)
    assert Job.objects.count() == 501
    assert source.ingestionrun_set.filter(complete=True).count() == 1


def test_request_capacity_cooldown_and_expired_leases(settings):
    settings.CRAWL_GLOBAL_CONCURRENCY = 2
    settings.CRAWL_HOST_CONCURRENCY = 1
    first = acquire("acme.example")
    with pytest.raises(CrawlDeferred):
        acquire("acme.example")
    acquire("other.example")
    with pytest.raises(CrawlDeferred):
        acquire("third.example")
    CrawlLease.objects.filter(token=first).update(expires_at=timezone.now() - timedelta(seconds=1))
    acquire("third.example")
    CrawlHost.objects.filter(host="acme.example").update(
        available_at=timezone.now() + timedelta(minutes=5)
    )
    with pytest.raises(CrawlDeferred):
        acquire("acme.example")


def test_admin_crud_permissions_curated_content_and_cascade(user, application):
    job_admin = admin.site._registry[Job]
    company_admin = admin.site._registry[Company]
    request = RequestFactory().get("/admin/")
    request.user = user
    assert not job_admin.has_add_permission(request)
    assert not job_admin.has_change_permission(request)
    assert not job_admin.has_delete_permission(request)
    with pytest.raises(PermissionDenied):
        company_admin.import_csv(request)
    user.user_permissions.add(
        *Permission.objects.filter(codename__in=["add_job", "change_job", "delete_job"])
    )
    user = type(user).objects.get(pk=user.pk)
    request.user = user
    assert job_admin.has_add_permission(request) and job_admin.has_change_permission(request)
    assert job_admin.has_delete_permission(request)
    obj = application.job
    obj.title = "Curated"
    job_admin.save_model(request, obj, Mock(changed_data=["title"]), True)
    assert obj.content_overrides == {"title": "Curated"}
    obj.status_override = "archived"
    obj.save()
    job_admin.resume_automatic_status(request, Job.objects.filter(pk=obj.pk))
    obj.refresh_from_db()
    assert obj.active
    job_pk = obj.pk
    application_pk = application.pk
    obj.delete()
    assert not Job.objects.filter(pk=job_pk).exists()
    assert not type(application).objects.filter(pk=application_pk).exists()
    free = Job.objects.create(title="Manual", canonical_url="https://manual.example/job")
    free.delete()
    assert not Job.objects.filter(pk=free.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_ingestion_lease(monkeypatch):
    source = employer()
    entered = threading.Event()
    release = threading.Event()

    def collect(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return CollectionResult([listing()], "acme", True), b""

    monkeypatch.setattr(ingestion, "collect", collect)

    def worker():
        close_old_connections()
        try:
            ingestion.ingest(source.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        assert entered.wait(timeout=5)
        second = pool.submit(worker)
        second.result(timeout=5)
        release.set()
        first.result(timeout=10)
    assert source.ingestionrun_set.count() == 1 and Job.objects.count() == 1


def test_schedule_10000_companies(monkeypatch, record_testsuite_property):
    started = time.monotonic()
    companies = Company.objects.bulk_create(
        [Company(name=f"Synthetic {i}", identity_key=f"synthetic-{i}") for i in range(10000)]
    )
    due = timezone.now() - timedelta(hours=1)
    Source.objects.bulk_create(
        [
            Source(
                kind="company",
                name=f"Synthetic {i}",
                company=company,
                url=f"https://synthetic-{i}.example/careers",
                adapter="jsonld",
                enabled=True,
                next_run_at=due,
            )
            for i, company in enumerate(companies)
        ]
    )
    dispatch = Mock()
    monkeypatch.setattr(tasks.ingest_source, "delay", dispatch)
    schedule_started = time.monotonic()
    for _ in range(100):
        tasks.schedule_sources()
    assert dispatch.call_count == 10000
    assert len({call.args[0] for call in dispatch.call_args_list}) == 10000
    tasks.schedule_sources()
    assert dispatch.call_count == 10000
    scheduler_seconds = time.monotonic() - schedule_started
    # Measure a bounded collection sample without any network access.
    for index, source in enumerate(Source.objects.order_by("pk")[:100]):
        result = CollectionResult(
            [
                Listing(
                    f"https://synthetic-{index}.example/job",
                    "Synthetic engineer",
                    provider_key=f"synthetic:{index}:1",
                )
            ],
            f"synthetic:{index}",
            True,
        )
        monkeypatch.setattr(ingestion, "collect", lambda *args, batch=result, **kw: (batch, b""))
        ingestion.ingest(source.pk)
    from jobs.models import IngestionRun

    runs = list(IngestionRun.objects.all())
    assert len(runs) == 100 and all(run.complete for run in runs)
    metrics = {
        "companies": 10000,
        "dispatches": dispatch.call_count,
        "scheduler_seconds": round(scheduler_seconds, 3),
        "total_seconds": round(time.monotonic() - started, 3),
        "synthetic_collection_runs": len(runs),
        "average_run_seconds": round(sum(run.duration_seconds for run in runs) / len(runs), 4),
        "average_queue_delay_seconds": round(
            sum(run.queue_delay_seconds for run in runs) / len(runs), 3
        ),
        "network_requests": sum(run.request_count for run in runs),
        "synthetic_completeness": sum(run.complete for run in runs) / len(runs),
        "synthetic_failure_rate": sum(run.status == "failed" for run in runs) / len(runs),
    }
    record_testsuite_property("catalogue_benchmark", json.dumps(metrics))
    print(json.dumps(metrics))


def test_endpoint_scope_changes_do_not_close_previous_inventory(monkeypatch):
    source = employer()
    run(source, monkeypatch, [listing()])
    old_job = Job.objects.get()
    Source.objects.filter(pk=source.pk).update(
        url=GH.replace("acme", "new"), next_run_at=timezone.now()
    )
    monkeypatch.setattr(
        ingestion, "collect", lambda *a, **kw: (CollectionResult([], "greenhouse:new:", True), b"")
    )
    for _ in range(2):
        Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
        ingestion.ingest(source.pk)
    old_job.refresh_from_db()
    assert old_job.active and old_job.origins.get().missing_count == 0


def test_workday_more_than_500_and_read_only_requests(monkeypatch):
    source = Source.objects.create(
        kind="company",
        company=Company.objects.create(
            name="Test employer", website="https://employer.example/", website_verified=True
        ),
        name="Workday",
        url=WD,
        adapter="workday",
        enabled=True,
        authoritative=True,
        employer_name="Acme",
    )
    request, calls = workday_request(501)
    monkeypatch.setattr(inventories, "request_public_document", request)
    for _ in range(51):
        Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
        ingestion.ingest(source.pk)
    source.refresh_from_db()
    assert Job.objects.count() == 501
    assert source.ingestionrun_set.filter(complete=True).count() == 1
    assert not source.inventory_cursor and source.last_complete_at
    searches = [kwargs["payload"] for url, kwargs in calls if url.endswith("/jobs")]
    assert [search["offset"] for search in searches] == list(range(0, 501, 10))
    assert all(search["appliedFacets"] == {} and search["searchText"] == "" for search in searches)
    assert len(calls) == 552  # 51 search POSTs, 501 detail GETs, no submission endpoint.


def test_network_429_shares_cooldown_without_replaying_post(monkeypatch):
    from jobs.services import network

    response = Mock(status=429)
    response.getheader.return_value = "120"
    transport = Mock()
    transport.getresponse.return_value = response
    constructor = Mock(return_value=transport)
    monkeypatch.setattr(network, "public_addresses", lambda host: ["8.8.8.8"])
    monkeypatch.setattr(network, "PinnedHTTPSConnection", constructor)
    with collection_context() as stats:
        with pytest.raises(HTTPStatusError):
            network.request_public_document(WD + "/jobs", payload={"offset": 0})
        with pytest.raises(CrawlDeferred):
            network.request_public_document(WD + "/jobs", payload={"offset": 0})
        assert stats["requests"] == 1
    assert transport.request.call_count == 1 and not CrawlLease.objects.exists()
    host = CrawlHost.objects.get()
    assert host.available_at > timezone.now() + timedelta(seconds=119)
    response.status = 302
    with pytest.raises(ValueError, match="redirect refused"):
        network.request_public_document(WD, payload={})


def test_explicit_filled_status_survives_absence(monkeypatch):
    source = employer()
    run(source, monkeypatch, [listing(employer_status="filled")])
    run(source, monkeypatch, [])
    run(source, monkeypatch, [])
    job = Job.objects.get()
    assert job.lifecycle == "filled" and job.origins.get().observed_status == "filled"


def test_crawl_report_and_import_command(monkeypatch, tmp_path):
    from django.core.management import call_command

    path = tmp_path / "companies.csv"
    path.write_text("name,website\nAcme,https://acme.example\n")
    output = StringIO()
    call_command("import_companies", str(path), stdout=output)
    assert "1 created" in output.getvalue()
    source = employer()
    run(source, monkeypatch, [listing()])
    output = StringIO()
    call_command("crawl_report", stdout=output)
    data = json.loads(output.getvalue())
    assert data["completed_inventories"] == 1 and data["failures"] == 0
    assert data["pending_company_reviews"] >= 1


def test_company_discovery_scheduler_and_task(monkeypatch):
    company = catalogue.verify_company(
        Company.objects.create(name="Acme", website="https://acme.example")
    )
    dispatch = Mock()
    monkeypatch.setattr(tasks.discover_company_careers, "delay", dispatch)
    tasks.schedule_company_discovery()
    tasks.schedule_company_discovery()
    dispatch.assert_called_once_with(company.pk)
    monkeypatch.setattr(catalogue, "discover_company", Mock(side_effect=ValueError("unsupported")))
    tasks.discover_company_careers(company.pk)
    company.refresh_from_db()
    assert company.discovery_error == "ValueError"
    monkeypatch.setattr(catalogue, "discover_company", Mock(return_value=[]))
    tasks.discover_company_careers(company.pk)
    company.refresh_from_db()
    assert company.discovery_error == "" and company.next_discovery_at > timezone.now() + timedelta(
        days=6
    )


def test_unverified_import_does_not_merge_different_employers():
    catalogue.import_companies(
        StringIO("name,website\nAcme,https://acme.example\nOther,https://acme.example\n")
    )
    assert Company.objects.count() == 2
    a, b = Company.objects.order_by("pk")
    catalogue.verify_company(a)
    catalogue.verify_company(b)
    assert Company.objects.count() == 1


def test_workday_detail_checkpoint_survives_capacity_or_429():
    source = Source(url=WD, max_response_bytes=1000000)
    for error in [
        CrawlDeferred(timezone.now() + timedelta(seconds=30)),
        HTTPStatusError(429, "120"),
    ]:
        base_request, calls = workday_request(3)
        paused = False

        def request(url, failure=error, fetch=base_request, **kwargs):
            nonlocal paused
            if url.endswith("Engineer_JR1") and not paused:
                paused = True
                raise failure
            return fetch(url, **kwargs)

        first = WorkdayAdapter().collect(source, {}, request=request)
        assert len(first.postings) == 1 and first.retry_at and not first.complete
        assert first.cursor["pending"] == ["/job/Remote/Engineer_JR1", "/job/Remote/Engineer_JR2"]
        last = WorkdayAdapter().collect(source, first.cursor, request=request)
        assert last.complete and len(last.postings) == 2
        assert len([url for url, kwargs in calls if url.endswith("/jobs")]) == 1


def test_stale_worker_runs_are_visible_and_recoverable(monkeypatch):
    from jobs.models import IngestionRun

    source = employer()
    stale = IngestionRun.objects.create(source=source)
    IngestionRun.objects.filter(pk=stale.pk).update(
        started_at=timezone.now() - timedelta(minutes=11)
    )
    dispatch = Mock()
    monkeypatch.setattr(tasks.ingest_source, "delay", dispatch)
    tasks.schedule_sources()
    stale.refresh_from_db()
    assert stale.status == "failed" and stale.error_code == "WorkerInterrupted"
    dispatch.assert_called_once_with(source.pk)


def test_admin_pages_and_csv_import_with_verified_staff_session(client, user, job):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django_otp import DEVICE_ID_SESSION_KEY

    user.is_staff = user.is_superuser = True
    user.save()
    device = user.totpdevice_set.create(name="synthetic-admin-test", confirmed=True)
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    for path in [
        "/admin/jobs/job/",
        f"/admin/jobs/job/{job.pk}/change/",
        "/admin/jobs/job/add/",
        "/admin/jobs/company/",
        "/admin/jobs/company/add/",
        "/admin/jobs/source/add/",
        "/admin/jobs/company/import-csv/",
    ]:
        assert client.get(path).status_code == 200
    csv = SimpleUploadedFile(
        "companies.csv", b"name,website\nAcme,https://acme.example\n", content_type="text/csv"
    )
    response = client.post("/admin/jobs/company/import-csv/", {"file": csv})
    assert response.status_code == 302
    assert Company.objects.get().name == "Acme" and not Company.objects.get().website_verified
