"""Durable, bounded employer search. No job ingestion or application side effects."""

import csv
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from jobs.models import (
    Company,
    EmployerDiscoveryBudget,
    EmployerDiscoveryCampaign,
    EmployerDiscoveryLead,
    EmployerDiscoveryRun,
)

from . import brave_search, pituchim
from .catalogue import domain, import_companies
from .crawl import CrawlDeferred, collection_context
from .discovery import ats_endpoint
from .employer_evidence import inspect, location_matches, role_matches
from .network import HTTPStatusError, canonical_url
from .retries import RetryWindowTooLong, retry_after_seconds

LEASE = timedelta(minutes=10)  # Longer than the worker hard limit.
MAX_RETRIES = 3


def tomorrow():
    now = timezone.now().astimezone(UTC)
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)


@transaction.atomic
def start(campaign_id: int, *, manual: bool = False) -> EmployerDiscoveryRun | None:
    campaign = EmployerDiscoveryCampaign.objects.select_for_update().get(pk=campaign_id)
    if not campaign.enabled:
        return None
    active = EmployerDiscoveryRun.objects.filter(campaign=campaign, status="active").first()
    if active:
        return active
    if not manual and campaign.next_run_at > timezone.now():
        return None
    previous = EmployerDiscoveryRun.objects.filter(campaign=campaign).order_by("-pk").first()
    run = EmployerDiscoveryRun.objects.create(
        campaign=campaign, pending_leads=previous.pending_leads if previous else []
    )
    campaign.next_run_at = tomorrow()
    campaign.save(update_fields=["next_run_at"])
    return run


@transaction.atomic
def reserve(campaign: EmployerDiscoveryCampaign, kind: str, *, amount: int = 1) -> bool:
    """Reserve before I/O, including retries; one lock bounds all campaigns together."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(720316014)")
    day = timezone.now().astimezone(UTC).date()
    bucket, _ = EmployerDiscoveryBudget.objects.get_or_create(campaign=campaign, day=day)
    buckets = EmployerDiscoveryBudget.objects.filter(day=day)
    search_buckets = (
        buckets.filter(campaign__provider="brave")
        if campaign.provider == "brave"
        else buckets.exclude(campaign__provider="brave")
    )
    totals = buckets.aggregate(searches=Sum("searches"), candidates=Sum("candidates"))
    limit = campaign.daily_search_limit if kind == "searches" else campaign.daily_candidate_limit
    global_limit = (30 if campaign.provider == "brave" else 500) if kind == "searches" else 100
    if kind == "searches":
        totals[kind] = search_buckets.aggregate(total=Sum("searches"))["total"] or 0
        if campaign.provider != "brave":
            provider_limit = {"pituchim": 100, "arbeitnow": 200, "himalayas": 200}[
                campaign.provider
            ]
            provider_total = (
                buckets.filter(campaign__provider=campaign.provider).aggregate(
                    total=Sum("searches")
                )["total"]
                or 0
            )
            if provider_total + amount > provider_limit:
                return False
    if getattr(bucket, kind) + amount > limit or (totals[kind] or 0) + amount > global_limit:
        return False
    setattr(bucket, kind, getattr(bucket, kind) + amount)
    bucket.save(update_fields=[kind])
    return True


@transaction.atomic
def promote(lead_id: int) -> bool:
    # New identities coordinate before locking the lead; existing-company inspections
    # already hold provenance/lead locks and never need the identity-creation lock.
    if EmployerDiscoveryLead.objects.filter(pk=lead_id, company__isnull=True).exists():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(720316015)")
    lead = (
        EmployerDiscoveryLead.objects.select_for_update(of=("self",))
        .select_related("campaign")
        .get(pk=lead_id)
    )
    if lead.status == "rejected":
        raise ValidationError("Rejected leads cannot be promoted.")
    required = (
        lead.name,
        lead.website,
        lead.career_url,
        lead.vacancy_title,
        lead.vacancy_location,
        lead.vacancy_url,
        lead.relationship_url,
        lead.evidence_kind,
    )
    now = timezone.now()
    if (
        not all(required)
        or not lead.observed_at
        or not now - timedelta(days=7) <= lead.observed_at <= now
    ):
        raise ValidationError("Record recent vacancy and employer relationship evidence first.")
    if lead.valid_until and lead.valid_until <= now:
        raise ValidationError("The vacancy evidence has expired.")
    if not role_matches(lead.vacancy_title, lead.campaign.technology_roles) or not location_matches(
        lead.vacancy_location, country_code=True
    ):
        raise ValidationError("A technology vacancy in a target region is required.")
    try:
        for url in (lead.website, lead.career_url, lead.vacancy_url, lead.relationship_url):
            if len(canonical_url(url)) > 2048:
                raise ValueError("URL too long")
        if domain(lead.relationship_url) != domain(lead.website):
            raise ValueError("Relationship evidence must be on the employer website")
        endpoint = ats_endpoint(lead.career_url)
        same_site = domain(lead.vacancy_url) == domain(lead.website)
        matched_board = endpoint is not None and ats_endpoint(lead.vacancy_url) == endpoint
        # An operator can attest an unsupported hosted system without implementing its adapter.
        career_path = urlsplit(lead.career_url).path.rstrip("/")
        vacancy_path = urlsplit(lead.vacancy_url).path
        manual_hosted = (
            lead.evidence_kind == "manual"
            and domain(lead.vacancy_url) == domain(lead.career_url)
            and (vacancy_path == career_path or vacancy_path.startswith(career_path + "/"))
        )
        if not (same_site or matched_board or manual_hosted):
            raise ValueError("Vacancy is unrelated to the employer career endpoint")
        if domain(lead.career_url) != domain(lead.website) and not (endpoint or manual_hosted):
            raise ValueError("Unsupported external career endpoint requires operator attestation")
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("Invalid employer/vacancy relationship URLs.") from exc
    if lead.company_id:
        from .catalogue import candidate

        company = lead.company
        if not company.website or domain(company.website) != domain(lead.website):
            raise ValidationError("Review the company website before proposing an endpoint.")
        candidate(company, lead.career_url, evidence=lead.relationship_url)
        lead.status, lead.diagnostic = "confirmed", ""
        lead.save(update_fields=["status", "diagnostic"])
        return False
    stream = StringIO()
    writer = csv.writer(stream)
    writer.writerow(["name", "website", "career_url"])
    writer.writerow([lead.name, lead.website, lead.career_url])
    stream.seek(0)
    # The identity-creation lock was acquired before the lead lock.
    created, _ = import_companies(stream)
    company = Company.objects.filter(official_domain=domain(lead.website)).first()
    if company is None:
        import unicodedata

        normalized = " ".join(unicodedata.normalize("NFKC", lead.name).casefold().split())
        key = hashlib.sha256(f"catalogue:{domain(lead.website)}:{normalized}".encode()).hexdigest()
        company = Company.objects.get(identity_key=key)
    lead.company = company
    lead.status = "confirmed"
    lead.diagnostic = ""
    lead.save(update_fields=["company", "status", "diagnostic"])
    return bool(created)


@transaction.atomic
def claim(run_id):
    # Lock order is campaign -> run -> lead throughout the worker.
    campaign_id = (
        EmployerDiscoveryRun.objects.filter(pk=run_id).values_list("campaign_id", flat=True).first()
    )
    if campaign_id is None:
        return None
    campaign = EmployerDiscoveryCampaign.objects.select_for_update().get(pk=campaign_id)
    run = EmployerDiscoveryRun.objects.select_for_update().get(pk=run_id)
    now = timezone.now()
    if (
        not campaign.enabled
        or run.status != "active"
        or run.next_attempt_at > now
        or (run.lease_until is not None and run.lease_until > now)
    ):
        return None
    run.lease_token = uuid.uuid4()
    run.lease_until = now + LEASE
    run.save(update_fields=["lease_token", "lease_until"])
    return run, campaign, run.lease_token


@transaction.atomic
def locked(run_id, token):
    """Callers hold these locks in an outer transaction until their checkpoint commits."""
    campaign_id = EmployerDiscoveryRun.objects.get(pk=run_id).campaign_id
    campaign = EmployerDiscoveryCampaign.objects.select_for_update().get(pk=campaign_id)
    run = EmployerDiscoveryRun.objects.select_for_update().get(pk=run_id)
    if (
        not campaign.enabled
        or run.status != "active"
        or run.lease_token != token
        or run.lease_until is None
        or run.lease_until <= timezone.now()
    ):
        return None
    return run, campaign


def complete(run, campaign, diagnostic):
    run.status = "complete"
    run.finished_at = timezone.now()
    run.diagnostic = diagnostic
    campaign.next_run_at = max(campaign.next_run_at, tomorrow())
    campaign.save(update_fields=["next_run_at"])


def release(run, *, delay=1):
    run.lease_token = run.lease_until = None
    run.next_attempt_at = timezone.now() + timedelta(seconds=delay)
    run.next_dispatch_at = run.next_attempt_at
    run.save()


@transaction.atomic
def prepare_step(run_id, token):
    pair = locked(run_id, token)
    if not pair:
        return None
    run, campaign = pair
    try:
        campaign.full_clean()
    except ValidationError:
        run.status, run.diagnostic = "failed", "InvalidCampaignConfiguration"
        run.finished_at = timezone.now()
        release(run)
        return None
    if campaign.provider == "brave" and not settings.BRAVE_SEARCH_API_KEY:
        run.status, run.diagnostic = "failed", "Configure BRAVE_SEARCH_API_KEY"
        run.finished_at = timezone.now()
        release(run)
        return None
    if campaign.catalogue_mode:
        if campaign.query_pages.get("catalogue_complete"):
            campaign.query_pages = {}
            campaign.directory_page = 1
            campaign.save(update_fields=["query_pages", "directory_page"])
            complete(run, campaign, "CatalogueCycleComplete")
            release(run)
            return None
        if not reserve(campaign, "searches"):
            complete(run, campaign, "DailyBudgetExhausted")
            release(run)
            return None
        run.searched += 1
        run.save(update_fields=["searched"])
        if campaign.provider == "pituchim":
            rules_fresh = (
                campaign.query_pages.get("pituchim_robots_page") == campaign.directory_page
                and campaign.query_pages.get("pituchim_robots_until", 0)
                > timezone.now().timestamp()
            )
            return (
                ("directory" if rules_fresh else "directory_rules"),
                campaign.directory_page,
                None,
            )
        return "feed", campaign.provider, campaign.query_pages
    if (
        campaign.provider == "pituchim"
        and not run.pending_leads
        and campaign.query_pages.get("pituchim_complete")
    ):
        campaign.directory_page = 1
        campaign.query_pages = {}
        campaign.save(update_fields=["directory_page", "query_pages"])
        complete(run, campaign, "DirectoryCycleComplete")
        release(run)
        return None
    if run.pending_leads:
        lead = EmployerDiscoveryLead.objects.get(pk=run.pending_leads[0], campaign=campaign)
        if lead.status == "rejected":
            run.pending_leads = run.pending_leads[1:]
            run.rejected += 1
            release(run)
            return None
    kind = "candidates" if run.pending_leads else "searches"
    amount = 1
    if not reserve(campaign, kind, amount=amount):
        complete(run, campaign, "DailyBudgetExhausted")
        release(run)
        return None
    if kind == "candidates":
        lead = EmployerDiscoveryLead.objects.get(pk=run.pending_leads[0], campaign=campaign)
        run.inspected += 1
        run.save(update_fields=["inspected"])
        return "candidate", lead, campaign.technology_roles
    if campaign.provider == "pituchim":
        run.searched += amount
        run.save(update_fields=["searched"])
        rules_fresh = (
            campaign.query_pages.get("pituchim_robots_page") == campaign.directory_page
            and campaign.query_pages.get("pituchim_robots_until", 0) > timezone.now().timestamp()
        )
        return ("directory" if rules_fresh else "directory_rules"), campaign.directory_page, None
    index = campaign.query_cursor % len(campaign.queries)
    query = campaign.queries[index]
    offset = campaign.query_pages.get(query, 0)
    if type(offset) is not int or not 0 <= offset <= 9:
        offset = 0
    run.searched += 1
    run.save(update_fields=["searched"])
    return "search", query, offset


@transaction.atomic
def save_search(run_id, token, query, offset, urls, more):
    pair = locked(run_id, token)
    if not pair:
        return
    run, campaign = pair
    pending = []
    for raw in urls:
        try:
            url = canonical_url(raw)
            if len(url) > 2048:
                continue
        except ValueError, UnicodeError:
            continue
        key = hashlib.sha256(url.encode()).hexdigest()
        lead, added = EmployerDiscoveryLead.objects.get_or_create(
            campaign=campaign,
            key=key,
            defaults={
                "run": run,
                "candidate_url": url,
                "search_query": query,
                "search_offset": offset,
            },
        )
        if lead.pk in pending:
            run.duplicate += 1
            continue
        if not added:
            run.duplicate += 1
        if lead.status == "rejected":
            run.rejected += 1
            continue
        if lead.observed_at and lead.observed_at >= timezone.now() - timedelta(days=1):
            continue
        lead.run, lead.search_query, lead.search_offset = run, query, offset
        lead.save(update_fields=["run", "search_query", "search_offset"])
        pending.append(lead.pk)
    run.pending_leads = pending
    campaign.query_cursor += 1
    campaign.query_pages = {
        key: value for key, value in campaign.query_pages.items() if key in campaign.queries
    }
    campaign.query_pages[query] = offset + 1 if more and offset < 9 else 0
    campaign.save(update_fields=["query_cursor", "query_pages"])
    run.retries, run.diagnostic = 0, ""
    release(run)


@transaction.atomic
def save_directory_rules(run_id, token, page):
    pair = locked(run_id, token)
    if not pair:
        return
    run, campaign = pair
    if campaign.provider != "pituchim" or campaign.directory_page != page:
        release(run)
        return
    campaign.query_pages = {
        "pituchim_robots_page": page,
        "pituchim_robots_until": (timezone.now() + timedelta(hours=1)).timestamp(),
    }
    campaign.save(update_fields=["query_pages"])
    run.retries, run.diagnostic = 0, ""
    release(run, delay=5)


@transaction.atomic
def save_directory(run_id, token, page, companies, more):
    pair = locked(run_id, token)
    if not pair:
        return
    run, campaign = pair
    if campaign.provider != "pituchim" or campaign.directory_page != page:
        release(run)
        return
    pending = []
    for company in companies:
        url = canonical_url(company.website)
        key = hashlib.sha256(url.encode()).hexdigest()
        lead, added = EmployerDiscoveryLead.objects.get_or_create(
            campaign=campaign,
            key=key,
            defaults={
                "run": run,
                "candidate_url": url,
                "search_query": pituchim.page_url(page),
                "search_offset": 0,
                "name": company.name,
                "website": url,
                "diagnostic": "DirectoryHiringBadgeUnverified",
            },
        )
        if not added or lead.pk in pending:
            run.duplicate += 1
        if lead.pk in pending:
            continue
        if lead.status == "rejected":
            run.rejected += 1
            continue
        if lead.observed_at and lead.observed_at >= timezone.now() - timedelta(days=1):
            continue
        lead.run = run
        lead.search_query = pituchim.page_url(page)
        lead.save(update_fields=["run", "search_query"])
        pending.append(lead.pk)
    run.pending_leads = pending
    campaign.directory_page = page + 1 if more else page
    campaign.query_pages = {} if more else {"pituchim_complete": True}
    campaign.save(update_fields=["directory_page", "query_pages"])
    run.retries, run.diagnostic = 0, ""
    release(run, delay=5)


@transaction.atomic
def save_candidate(run_id, token, lead_id, evidence, diagnostic):
    pair = locked(run_id, token)
    if not pair:
        return
    run, _campaign = pair
    lead = EmployerDiscoveryLead.objects.select_for_update().get(pk=lead_id)
    if lead.status == "rejected":
        run.rejected += 1
    else:
        for field, value in evidence.fields().items():
            # Preserve directory identity hints when hiring verification is inconclusive.
            if field in {"name", "website"} and not value:
                continue
            setattr(lead, field, value)
        lead.status = "unresolved"
        lead.diagnostic = diagnostic
        lead.observed_at = timezone.now()
        lead.save()
        if evidence.evidence_kind:
            try:
                created = promote(lead.pk)
            except ValidationError:
                lead.diagnostic = "EvidenceRequiresReview"
                lead.save(update_fields=["diagnostic"])
                run.unresolved += 1
            else:
                run.confirmed += 1
                run.duplicate += int(not created)
        else:
            run.unresolved += 1
    run.pending_leads = run.pending_leads[1:]
    run.retries, run.diagnostic = 0, ""
    release(run)


@transaction.atomic
def failure(run_id, token, exc):
    pair = locked(run_id, token)
    if not pair:
        return
    run, campaign = pair
    run.diagnostic = (
        f"HTTP_{exc.status}" if isinstance(exc, HTTPStatusError) else type(exc).__name__[:80]
    )  # Never store exception payloads/provider bodies.
    transient = isinstance(exc, (CrawlDeferred, httpx.TransportError, OSError)) or (
        isinstance(exc, HTTPStatusError) and exc.status in {429, 500, 502, 503, 504}
    )
    retry_delay = 60 * (2 ** min(run.retries, MAX_RETRIES))
    if transient and isinstance(exc, HTTPStatusError):
        try:
            retry_delay = max(retry_delay, retry_after_seconds(exc.retry_after, retry_delay))
        except RetryWindowTooLong:
            transient = False
            run.diagnostic = "RetryWindowTooLong"
    run.retries += 1
    if transient and run.retries <= MAX_RETRIES:
        delay = retry_delay
        if isinstance(exc, CrawlDeferred):
            delay = max(delay, (exc.until - timezone.now()).total_seconds())
        release(run, delay=min(delay, 365 * 86400))
        return
    run.failed += 1
    if run.pending_leads:
        lead = EmployerDiscoveryLead.objects.select_for_update().get(pk=run.pending_leads[0])
        if lead.status != "rejected":
            lead.status = "unresolved"
            lead.diagnostic = run.diagnostic
            # Failed verification cannot leave promotable stale evidence.
            lead.evidence_kind = ""
            lead.save(update_fields=["status", "diagnostic", "evidence_kind"])
            run.unresolved += 1
        run.pending_leads = run.pending_leads[1:]
        run.retries = 0
    else:
        run.status = "failed"
        run.finished_at = timezone.now()
        campaign.next_run_at = max(campaign.next_run_at, tomorrow())
        campaign.save(update_fields=["next_run_at"])
    release(run)


def step(run_id: int) -> None:
    claimed = claim(run_id)
    if claimed is None:
        return
    _run, _campaign, token = claimed
    operation = prepare_step(run_id, token)
    if operation is None:
        return
    kind, value, arg = operation
    try:
        with collection_context():
            if kind == "search":
                urls, more = brave_search.search(value, arg)
                save_search(run_id, token, value, arg, urls, more)
            elif kind == "directory_rules":
                pituchim.check_robots(value)
                save_directory_rules(run_id, token, value)
            elif kind == "feed":
                raise ValueError("Job boards must use job ingestion, not company discovery")
            elif kind == "directory":
                if _campaign.catalogue_mode:
                    from . import catalogue_discovery

                    companies, more = pituchim.fetch(
                        value,
                        robots_approved=True,
                        hiring_only=_campaign.hiring_only,
                    )
                    catalogue_discovery.save_page(
                        run_id,
                        token,
                        catalogue_discovery.directory_page(companies, value, more),
                    )
                else:
                    companies, more = pituchim.fetch(value, robots_approved=True)
                    save_directory(run_id, token, value, companies, more)
            else:
                if _campaign.provider == "pituchim":
                    evidence, diagnostic = inspect(
                        value.candidate_url,
                        arg,
                        company_hint=value.name,
                        website_hint=value.website,
                    )
                else:
                    evidence, diagnostic = inspect(value.candidate_url, arg)
                save_candidate(run_id, token, value.pk, evidence, diagnostic)
    except Exception as exc:
        # Persist a visible failure, including unexpected parser errors, without leaking content.
        failure(run_id, token, exc)


def dispatch() -> None:
    now = timezone.now()
    due = EmployerDiscoveryCampaign.objects.filter(enabled=True, next_run_at__lte=now)
    for pk in due.order_by("next_run_at", "pk").values_list("pk", flat=True)[:100]:
        start(pk)
    from jobs.tasks import discover_employers

    now = timezone.now()
    with transaction.atomic():
        runs = list(
            EmployerDiscoveryRun.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(
                status="active",
                campaign__enabled=True,
                next_attempt_at__lte=now,
                next_dispatch_at__lte=now,
            )
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
            .order_by("pk")[:100]
        )
        for run in runs:
            run.next_dispatch_at = now + timedelta(minutes=5)
            run.save(update_fields=["next_dispatch_at"])
    for run in runs:
        discover_employers.delay(run.pk)
