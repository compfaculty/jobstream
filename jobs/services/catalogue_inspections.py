"""Independent, bounded inspection of imported employer websites; never attest identity."""

import uuid
from datetime import UTC, timedelta

import httpx
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, Exists, F, IntegerField, OuterRef, Q, Sum, Value, When
from django.utils import timezone

from jobs.models import Company, CompanyProvenance, EmployerDiscoveryBudget, EmployerDiscoveryLead

from . import employer_discovery as discovery
from .crawl import CrawlDeferred, collection_context
from .employer_evidence import inspect
from .network import HTTPStatusError
from .retries import RetryWindowTooLong, retry_after_seconds


def lock_record(pk):
    company_id = (
        CompanyProvenance.objects.filter(pk=pk).values_list("company_id", flat=True).first()
    )
    if company_id is None:
        return None
    company = Company.objects.select_for_update(no_key=True).filter(pk=company_id).first()
    row = (
        CompanyProvenance.objects.select_for_update(no_key=True)
        .filter(pk=pk, company_id=company_id)
        .first()
    )
    return (row, company) if row is not None and company is not None else None


@transaction.atomic
def claim(pk):
    pair = lock_record(pk)
    if pair is None:
        return None
    row, company = pair
    campaign = row.campaign
    now = timezone.now()
    if (
        not campaign.enabled
        or not campaign.catalogue_mode
        or not company.website
        or row.next_check_at > now
    ):
        return None
    if CompanyProvenance.objects.filter(company=company, lease_until__gt=now).exists():
        return None
    lead = (
        row.observations.exclude(status="rejected")
        .order_by("-hiring_signal", "-reported_at", "pk")
        .first()
    )
    if lead is None:
        return None
    try:
        campaign.full_clean()
    except ValidationError:
        row.diagnostic = "InvalidCampaignConfiguration"
        row.next_check_at = now + timedelta(days=1)
        row.save(update_fields=["diagnostic", "next_check_at"])
        return None
    if not discovery.reserve(campaign, "candidates"):
        CompanyProvenance.objects.filter(company=company).update(next_check_at=discovery.tomorrow())
        return None
    row.source_lead = lead
    row.lease_token = uuid.uuid4()
    row.lease_until = now + discovery.LEASE
    row.save(update_fields=["source_lead", "lease_token", "lease_until"])
    return row, company, row.lease_token


@transaction.atomic
def finish(pk, token, evidence=None, diagnostic="", exc=None):
    pair = lock_record(pk)
    if pair is None:
        return
    row, company = pair
    if (
        row.lease_token != token
        or row.lease_until is None
        or row.lease_until <= timezone.now()
        or not row.campaign.enabled
        or not row.campaign.catalogue_mode
    ):
        return
    delay = timedelta(days=7)
    if exc is not None:
        diagnostic = (
            f"HTTP_{exc.status}" if isinstance(exc, HTTPStatusError) else type(exc).__name__[:80]
        )
        transient = isinstance(exc, (CrawlDeferred, OSError, httpx.TransportError)) or (
            isinstance(exc, HTTPStatusError) and exc.status in {429, 500, 502, 503, 504}
        )
        row.retries += 1
        if transient and row.retries <= discovery.MAX_RETRIES:
            seconds = 60 * (2 ** (row.retries - 1))
            try:
                if isinstance(exc, HTTPStatusError):
                    seconds = retry_after_seconds(exc.retry_after, seconds)
                if isinstance(exc, CrawlDeferred):
                    seconds = max(seconds, (exc.until - timezone.now()).total_seconds())
            except RetryWindowTooLong:
                diagnostic = "RetryWindowTooLong"
                row.retries = 0
            else:
                delay = timedelta(seconds=min(seconds, 365 * 86400))
        else:
            row.retries = 0
    else:
        row.retries = 0
        lead = (
            EmployerDiscoveryLead.objects.select_for_update().filter(pk=row.source_lead_id).first()
        )
        if lead is not None and lead.status != "rejected" and evidence is not None:
            for field, value in evidence.fields().items():
                if field in {"name", "website"} and not value:
                    continue
                setattr(lead, field, value)
            lead.observed_at = timezone.now()
            lead.status = "unresolved"
            lead.diagnostic = diagnostic
            lead.save()
            if evidence.evidence_kind:
                try:
                    discovery.promote(lead.pk)
                except ValidationError:
                    diagnostic = "EvidenceRequiresReview"
                    lead.diagnostic = diagnostic
                    lead.save(update_fields=["diagnostic"])
    row.diagnostic = diagnostic
    row.lease_token = row.lease_until = None
    row.last_inspected_at = timezone.now()
    row.save()
    # All source identities for this company share one website-check schedule.
    CompanyProvenance.objects.filter(company=company).update(
        next_check_at=timezone.now() + delay,
        next_dispatch_at=timezone.now() + delay,
    )


def step(pk: int) -> None:
    claimed = claim(pk)
    if claimed is None:
        return
    row, company, token = claimed
    try:
        with collection_context():
            evidence, diagnostic = inspect(
                company.website,
                row.campaign.technology_roles,
                company_hint=company.name,
                website_hint=company.website,
            )
    except Exception as exc:
        finish(pk, token, exc=exc)
    else:
        finish(pk, token, evidence, diagnostic)


def dispatch() -> None:
    from jobs.tasks import inspect_catalogue_company

    now = timezone.now()
    spent = (
        EmployerDiscoveryBudget.objects.filter(day=now.astimezone(UTC).date()).aggregate(
            total=Sum("candidates")
        )["total"]
        or 0
    )
    if spent >= 100:
        return
    fresh = now - timedelta(days=7)
    eligible = (
        CompanyProvenance.objects.filter(
            campaign__enabled=True,
            campaign__catalogue_mode=True,
            next_check_at__lte=now,
            next_dispatch_at__lte=now,
        )
        .exclude(company__website="")
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    )
    eligible = eligible.annotate(
        has_observation=Exists(
            EmployerDiscoveryLead.objects.filter(provenance_id=OuterRef("pk")).exclude(
                status="rejected"
            )
        )
    ).filter(has_observation=True)
    priority = Case(
        When(
            Q(hiring_observed_at__gte=fresh)
            & (
                Q(source_lead__reported_expiry__isnull=True)
                | Q(source_lead__reported_expiry__gt=now)
            ),
            then="priority",
        ),
        default=Value(0),
        output_field=IntegerField(),
    )
    with transaction.atomic():
        rows = list(
            eligible.annotate(current_priority=priority)
            .select_for_update(skip_locked=True, of=("self",))
            .order_by("-current_priority", F("last_inspected_at").asc(nulls_first=True), "pk")[
                :1000
            ]
        )
        scheduled, companies = [], set()
        for row in rows:
            if row.company_id in companies:
                continue
            row.next_dispatch_at = now + timedelta(minutes=5)
            row.save(update_fields=["next_dispatch_at"])
            scheduled.append(row.pk)
            companies.add(row.company_id)
            if len(scheduled) == 100:
                break
    for pk in scheduled:
        inspect_catalogue_company.delay(pk)
