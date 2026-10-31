"""Database request leases and host cooldowns shared by ingestion workers."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from jobs.models import CrawlHost, CrawlLease

stats = ContextVar("crawl_stats", default=None)


class CrawlDeferred(Exception):
    def __init__(self, until):
        self.until = until
        super().__init__("crawl_capacity_or_cooldown")


@contextmanager
def collection_context():
    values = {"requests": 0}
    token = stats.set(values)
    try:
        yield values
    finally:
        stats.reset(token)


@transaction.atomic
def acquire(host):
    # One short coordination lock avoids races across global and host counters.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(720316001)")
    now = timezone.now()
    CrawlLease.objects.filter(expires_at__lte=now).delete()
    bucket, _ = CrawlHost.objects.get_or_create(host=host, defaults={"available_at": now})
    if bucket.available_at > now:
        raise CrawlDeferred(bucket.available_at)
    leases = CrawlLease.objects.all()
    if leases.count() >= settings.CRAWL_GLOBAL_CONCURRENCY or (
        leases.filter(host=host).count() >= settings.CRAWL_HOST_CONCURRENCY
    ):
        raise CrawlDeferred(now + timedelta(seconds=15))
    lease = CrawlLease.objects.create(host=host, expires_at=now + timedelta(seconds=360))
    bucket.available_at = now + timedelta(seconds=settings.CRAWL_HOST_INTERVAL)
    bucket.save(update_fields=["available_at"])
    return lease.token


@transaction.atomic
def cooldown(host, seconds):
    CrawlHost.objects.get_or_create(host=host)
    bucket = CrawlHost.objects.select_for_update().get(host=host)
    bucket.available_at = max(bucket.available_at, timezone.now() + timedelta(seconds=seconds))
    bucket.save(update_fields=["available_at"])


@contextmanager
def request_slot(host):
    values = stats.get()
    if values is None:
        yield
        return
    token = acquire(host)
    values["requests"] += 1
    try:
        yield
    finally:
        CrawlLease.objects.filter(token=token).delete()
