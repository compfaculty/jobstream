import hashlib
import http.client
import logging
import time
from dataclasses import replace
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from jobs.models import IngestionRun, Job, JobOrigin, Source

from .ai import AIUnavailable
from .ats import provider_identity
from .companies import resolve_company
from .contacts import observe
from .discovery import from_listing
from .network import fetch_public
from .normalization import normalize_with_ai
from .sources import ADAPTERS, BoardAdapter, RealWorkRSSAdapter, normalize

logger = logging.getLogger(__name__)


def ingest(source_id: int) -> None:
    # Database lease: one bounded run per source, even when Celery redelivers.
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_id)
        now = timezone.now()
        if not source.enabled or source.discovery_pending or source.next_run_at > now:
            return
        source.last_started_at = now
        source.next_run_at = now + timedelta(minutes=10)
        source.save(update_fields=["last_started_at", "next_run_at"])
        run = IngestionRun.objects.create(source=source)
    try:
        adapter = ADAPTERS[source.adapter]
        if source.authoritative and not source.employer_name.strip():
            raise ValueError("Authoritative source requires an employer name")
        data = (
            fetch_public(source.url)
            if source.max_response_bytes == 2 * 1024 * 1024
            else fetch_public(source.url, max_bytes=source.max_response_bytes)
        )
        limit = min(source.max_items, 500)
        listings = adapter.discover(data, source.url, limit)
        observed_keys = set()
        for index, listing in enumerate(listings):
            try:
                adapter = ADAPTERS[source.adapter]
                if isinstance(adapter, (BoardAdapter, RealWorkRSSAdapter)):
                    if index >= 10 and isinstance(adapter, BoardAdapter):
                        break  # Bound page-only runs; RSS can still import remaining feed entries.
                    if index < 10:
                        time.sleep(0.5)
                        try:
                            listing = adapter.metadata(listing, fetch_public(listing.url))
                        except ValueError, TypeError, OSError, http.client.HTTPException:
                            if not isinstance(adapter, RealWorkRSSAdapter):
                                raise
                            # Feed data is useful even when a detail page is unavailable.
                            run.enrichment_failed += 1
                if source.employer_name:
                    listing = replace(listing, company=source.employer_name)
                values = normalize(listing)
                if source.ai_normalization and not (
                    values["normalized"]["requirements"] or values["normalized"]["responsibilities"]
                ):
                    try:
                        values["normalized"] = normalize_with_ai(
                            values["description"], values["normalized"]
                        )
                    except AIUnavailable:
                        run.enrichment_failed += 1
                key = (
                    listing.provider_key
                    or provider_identity(listing.apply_url)
                    or provider_identity(listing.url)
                )
                if key:
                    values["provider_key"] = key
            except ValueError, TypeError, OSError, http.client.HTTPException:
                run.rejected += 1
                continue
            url = values.pop("canonical_url")
            with transaction.atomic():
                if key:
                    # Serialize equivalent ATS vacancies across concurrently ingested URLs.
                    lock = int.from_bytes(
                        hashlib.sha256(key.encode()).digest()[:8], "big", signed=True
                    )
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock])
                query = Q(canonical_url=url)
                if key:
                    query |= Q(provider_key=key)
                existing = Job.objects.select_for_update().filter(query).first()
                if existing:
                    url = existing.canonical_url
                    if existing.employer_source_id and not source.authoritative:
                        # Keep employer content authoritative while retaining board observations.
                        for field in (
                            "title",
                            "company",
                            "location",
                            "description",
                            "normalized",
                            "apply_url",
                            "posted_at",
                        ):
                            values[field] = getattr(existing, field)
                if source.authoritative:
                    values.update(
                        employer_source=source, verification="open", verified_at=timezone.now()
                    )
                if key:
                    observed_keys.add(key)
                profile = (
                    existing.company_profile
                    if existing
                    and existing.company_profile_id
                    and existing.company.casefold().strip() == values["company"].casefold().strip()
                    else resolve_company(values["company"], source)
                )
                job, _ = Job.objects.update_or_create(
                    canonical_url=url,
                    defaults={
                        **values,
                        "company_profile": profile,
                        "active": source.authoritative
                        or not existing
                        or existing.verification != "closed",
                        "last_seen_at": timezone.now(),
                    },
                )
                if not job.employer_source_id or source.authoritative:
                    observe(job, source, listing.url, listing.description)
                from_listing(source, listing)
                origin, created = JobOrigin.objects.update_or_create(
                    job=job,
                    source=source,
                    defaults={"source_url": listing.url, "last_seen_at": timezone.now()},
                )
                if listing.posted_at and (
                    origin.last_published_at is None or listing.posted_at > origin.last_published_at
                ):
                    if not created and origin.last_published_at:
                        origin.repost_count += 1
                    origin.last_published_at = listing.posted_at
                    origin.save(update_fields=["last_published_at", "repost_count"])
            run.imported += 1
        if (
            source.authoritative
            and run.rejected == 0
            and hasattr(adapter, "complete")
            and adapter.complete(data, source.url, limit)
        ):
            scope = adapter.scope(source.url)
            Job.objects.filter(
                Q(employer_source=source) | Q(provider_key__startswith=scope)
            ).exclude(provider_key__in=observed_keys).update(
                active=False,
                verification="closed",
                verified_at=timezone.now(),
                employer_source=source,
            )
        source.last_success_at = timezone.now()
        source.failure_count = 0
        source.next_run_at = timezone.now() + timedelta(minutes=source.interval_minutes)
        run.status = "success"
    except Exception as exc:
        # Record a category, never source response bodies, URLs with tokens, or credentials.
        run.status = "failed"
        run.error_code = type(exc).__name__[:80]
        source.failure_count += 1
        source.next_run_at = timezone.now() + timedelta(
            minutes=min(15 * 2 ** min(source.failure_count, 6), 1440)
        )
        logger.warning(
            "ingestion_failed", extra={"source_id": source.pk, "error_code": run.error_code}
        )
    finally:
        run.finished_at = timezone.now()
        run.save()
        source.save(update_fields=["last_success_at", "failure_count", "next_run_at"])
