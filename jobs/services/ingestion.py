"""Leased, resumable employer inventories and supplementary board observations."""

import hashlib
import http.client
import json
import logging
import random
import time
import uuid
from dataclasses import replace
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from jobs.models import IngestionRun, Job, JobOrigin, Source

from .ai import AIUnavailable
from .ats import provider_identity
from .companies import resolve_company
from .contacts import observe
from .crawl import CrawlDeferred, collection_context
from .discovery import from_listing
from .inventories import collect, source_adapter
from .lifecycle import CONTENT_FIELDS, complete_inventory, reconcile
from .network import HTTPStatusError, fetch_public
from .normalization import needs_ai_normalization, normalize_with_ai
from .retries import retry_after_seconds
from .sources import ADAPTERS, BoardAdapter, RealWorkRSSAdapter, normalize

logger = logging.getLogger(__name__)


def configuration(source: Source) -> str:
    value = [
        source.url,
        source.adapter,
        source.kind,
        source.authoritative,
        source.company_id,
        source.employer_name,
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def ingest(source_id: int) -> None:
    started = time.monotonic()
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_id)
        now = timezone.now()
        if (
            not source.enabled
            or source.discovery_pending
            or source.next_run_at > now
            or (source.lease_until and source.lease_until > now)
        ):
            return
        lease = uuid.uuid4()
        source.lease_token = lease
        source.lease_until = now + timedelta(minutes=10)
        source.last_started_at = now
        fingerprint = configuration(source)
        if (
            not source.inventory_token
            or source.inventory_config != fingerprint
            or not source.inventory_started_at
            or source.inventory_started_at < now - timedelta(days=2)
        ):
            source.inventory_token = uuid.uuid4()
            source.inventory_cursor = {}
            source.inventory_started_at = now
        source.inventory_config = fingerprint
        source.save()
        run = IngestionRun.objects.create(
            source=source, queue_delay_seconds=max(0, (now - source.next_run_at).total_seconds())
        )
    request_stats = {"requests": 0}
    try:
        source.full_clean()
        if source.authoritative and not source.employer_name.strip():
            raise ValueError("Authoritative source requires an employer name")
        with collection_context() as request_stats:
            result, _data = collect(source, source.inventory_cursor, fetch=fetch_public)
            run.scope = result.scope[:300]
            prepared = []
            adapter = (
                source_adapter(source)
                if source.adapter == "akeyless"
                else ADAPTERS.get(source.adapter)
            )
            seen = set()
            for index, listing in enumerate(result.postings):
                try:
                    if isinstance(adapter, (BoardAdapter, RealWorkRSSAdapter)):
                        if index >= 10 and isinstance(adapter, BoardAdapter):
                            break
                        if index < 10:
                            time.sleep(0.5)
                            try:
                                listing = adapter.metadata(listing, fetch_public(listing.url))
                            except ValueError, TypeError, OSError, http.client.HTTPException:
                                if not isinstance(adapter, RealWorkRSSAdapter):
                                    raise
                                run.enrichment_failed += 1
                    if source.employer_name:
                        listing = replace(listing, company=source.employer_name)
                    if listing.employer_status not in {"open", "closed", "filled"}:
                        raise ValueError("Invalid employer status")
                    values = normalize(listing)
                    if source.adapter in {"arbeitnow", "himalayas"} and listing.apply_url:
                        # Public feed adapters already validated this exact provider route.
                        values["apply_url"] = listing.apply_url
                    if (
                        not settings.JOB_NORMALIZATION_BATCH_ENABLED
                        and (source.ai_normalization or settings.JOB_AI_NORMALIZATION_ENABLED)
                        and (needs_ai_normalization(values["normalized"]))
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
                    if source.kind == Source.Kind.BOARD:
                        key = (
                            f"board:{source.pk}:"
                            + hashlib.sha256(values["canonical_url"].encode()).hexdigest()
                        )
                    identity = key or values["canonical_url"]
                    if identity in seen:
                        raise ValueError("Duplicate vacancy in inventory")
                    seen.add(identity)
                    if key:
                        values["provider_key"] = key
                    prepared.append((listing, values, key))
                except ValueError, TypeError, OSError, http.client.HTTPException:
                    run.rejected += 1
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(720316003)")
            current = Source.objects.select_for_update().get(pk=source_id)
            if (
                current.lease_token != lease
                or not current.enabled
                or current.discovery_pending
                or configuration(current) != fingerprint
            ):
                raise ValueError("Collection lease or configuration changed")
            # Operator rollout controls can change while public requests are in flight.
            source.closure_enabled = current.closure_enabled
            for listing, values, key in prepared:
                url = values.pop("canonical_url")
                identity = key or url
                lock = int.from_bytes(
                    hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True
                )
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock])
                query = Q(canonical_url=url)
                if key:
                    query |= Q(provider_key=key)
                existing = Job.objects.select_for_update().filter(query).first()
                imported = {field: values.get(field) for field in CONTENT_FIELDS}
                # JSON snapshots use ISO timestamps rather than datetime objects.
                if imported["posted_at"]:
                    imported["posted_at"] = imported["posted_at"].isoformat()
                if existing and existing.company_profile_id and source.kind == Source.Kind.BOARD:
                    run.rejected += 1
                    continue
                if existing and existing.employer_source_id and not source.authoritative:
                    for field in CONTENT_FIELDS:
                        values[field] = getattr(existing, field)
                else:
                    values["imported_content"] = imported
                if existing:
                    for field, value in existing.content_overrides.items():
                        if field in CONTENT_FIELDS:
                            values[field] = value
                if source.authoritative:
                    values.update(employer_source=source, verified_at=timezone.now())
                profile = resolve_company(values["company"], source)
                # Board observations never attach or create catalogue employers.
                if source.kind == Source.Kind.BOARD:
                    values["employer_source"] = None
                job, _ = Job.objects.update_or_create(
                    canonical_url=existing.canonical_url if existing else url,
                    defaults={**values, "company_profile": profile, "last_seen_at": timezone.now()},
                )
                if settings.JOB_NORMALIZATION_BATCH_ENABLED and (
                    source.ai_normalization or settings.JOB_AI_NORMALIZATION_ENABLED
                ):
                    from .normalization_batch import enqueue

                    enqueue(job)
                if not job.employer_source_id or source.authoritative:
                    observe(job, source, listing.url, listing.description)
                from_listing(source, listing)
                origin, created = JobOrigin.objects.update_or_create(
                    job=job,
                    source=source,
                    defaults={
                        "source_url": listing.url,
                        "last_seen_at": timezone.now(),
                        "inventory_token": source.inventory_token,
                        "missing_count": 0,
                        "observed_status": listing.employer_status,
                        "scope": result.scope,
                        "authoritative_observation": source.authoritative,
                    },
                )
                if listing.posted_at and (
                    origin.last_published_at is None or listing.posted_at > origin.last_published_at
                ):
                    if not created and origin.last_published_at:
                        origin.repost_count += 1
                    origin.last_published_at = listing.posted_at
                    origin.save(update_fields=["last_published_at", "repost_count"])
                if source.authoritative:
                    reconcile(job, source, run)
                run.imported += 1
            complete = result.complete and not result.errors and run.rejected == 0
            if complete and result.expected_total is not None:
                count = JobOrigin.objects.filter(
                    source=source, inventory_token=source.inventory_token
                ).count()
                complete = count == result.expected_total
            if complete:
                complete_inventory(source, source.inventory_token, run)
                source.last_complete_at = timezone.now()
                run.complete = True
            source.last_success_at = timezone.now()
            source.failure_count = 0
            source.last_error = ""
            continuing = bool(result.cursor) and not run.rejected and not result.errors
            source.inventory_cursor = result.cursor if continuing else {}
            if not continuing:
                source.inventory_token = None
                source.inventory_started_at = None
            source.next_run_at = timezone.now() + (
                timedelta(seconds=15)
                if continuing
                else timedelta(
                    # Scheduling jitter needs no cryptographic unpredictability.
                    minutes=source.interval_minutes * random.uniform(0.95, 1.05)  # noqa: S311
                )
            )
            if continuing and result.retry_at:
                source.next_run_at = max(source.next_run_at, result.retry_at)
            run.status = (
                "deferred"
                if result.retry_at
                else "success"
                if complete or not source.authoritative
                else "partial"
            )
            if result.errors:
                run.error_code = "collection_errors"
            elif run.rejected:
                run.error_code = "rejected_postings"
            source.lease_token = None
            source.lease_until = None
            # Preserve operator changes to unrelated source fields.
            fields = [
                "last_success_at",
                "last_complete_at",
                "failure_count",
                "last_error",
                "inventory_cursor",
                "inventory_token",
                "inventory_started_at",
                "next_run_at",
                "lease_token",
                "lease_until",
            ]
            Source.objects.filter(pk=source_id, lease_token=lease).update(
                **{field: getattr(source, field) for field in fields}
            )
            run.request_count = request_stats["requests"]
            run.duration_seconds = time.monotonic() - started
            run.finished_at = timezone.now()
            run.save()
    except Exception as exc:
        # A failed persistence transaction must not report rolled-back imports or closures.
        run.imported = 0
        run.would_close = 0
        run.complete = False
        run.status = "deferred" if isinstance(exc, CrawlDeferred) else "failed"
        run.error_code = type(exc).__name__[:80]
        with transaction.atomic():
            current = Source.objects.select_for_update().get(pk=source_id)
            if current.lease_token == lease:
                current.last_error = run.error_code
                if isinstance(exc, CrawlDeferred):
                    current.next_run_at = exc.until
                else:
                    current.failure_count += 1
                    delay = min(900 * 2 ** min(current.failure_count, 6), 86400)
                    if isinstance(exc, HTTPStatusError) and exc.status in {429, 503}:
                        delay = retry_after_seconds(exc.retry_after, delay)
                    else:
                        current.inventory_cursor = {}
                        current.inventory_token = None
                        current.inventory_started_at = None
                    current.next_run_at = timezone.now() + timedelta(seconds=delay)
                current.lease_token = None
                current.lease_until = None
                current.save(
                    update_fields=[
                        "last_error",
                        "next_run_at",
                        "failure_count",
                        "inventory_cursor",
                        "inventory_token",
                        "inventory_started_at",
                        "lease_token",
                        "lease_until",
                    ]
                )
        logger.warning(
            "ingestion_failed", extra={"source_id": source_id, "error_code": run.error_code}
        )
    finally:
        run.request_count = request_stats["requests"]
        run.duration_seconds = time.monotonic() - started
        run.finished_at = timezone.now()
        run.save()
