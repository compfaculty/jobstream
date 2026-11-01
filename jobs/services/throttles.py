"""Database-coordinated spacing shared by all workers; never sleep in a worker."""

import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from jobs.models import DeliveryThrottle


def scopes(connection, application):
    if application.channel == "portal":
        from .cato_greenhouse import supported

        if supported(application):
            key = hashlib.sha256(b"provider:cato_greenhouse").hexdigest()
            return [(key, getattr(settings, "CATO_APPLY_INTERVAL", 30.0))]
        key = hashlib.sha256(b"provider:secret_telaviv").hexdigest()
        return [(key, getattr(settings, "SECRET_TLV_APPLY_INTERVAL", 10.0))]
    values = [
        ("provider:gmail", getattr(settings, "SEND_PROVIDER_INTERVAL", 0.5)),
        (f"account:{connection.pk}", getattr(settings, "SEND_ACCOUNT_INTERVAL", 2.0)),
        (
            f"domain:{application.recipient.rsplit('@', 1)[-1].casefold()}",
            getattr(settings, "SEND_DOMAIN_INTERVAL", 5.0),
        ),
    ]
    return sorted((hashlib.sha256(key.encode()).hexdigest(), seconds) for key, seconds in values)


def reserve(connection, application) -> bool:
    """Caller holds the application row lock and a transaction."""
    buckets = []
    for key, seconds in scopes(connection, application):
        DeliveryThrottle.objects.get_or_create(key=key)
        bucket = DeliveryThrottle.objects.select_for_update().get(key=key)
        buckets.append((bucket, seconds))
    now = timezone.now()
    available = max(bucket.available_at for bucket, _ in buckets)
    if available > now:
        application.next_dispatch_at = available
        application.error_code = "delivery_throttled"
        application.save(update_fields=["next_dispatch_at", "error_code"])
        return False
    for bucket, seconds in buckets:
        bucket.available_at = now + timedelta(seconds=max(0, seconds))
        bucket.save(update_fields=["available_at"])
    return True


def defer_account(owner_id, until):
    from jobs.models import GmailConnection

    connection = GmailConnection.objects.filter(owner_id=owner_id).first()
    if connection:
        key = hashlib.sha256(f"account:{connection.pk}".encode()).hexdigest()
        DeliveryThrottle.objects.get_or_create(key=key)
        bucket = DeliveryThrottle.objects.select_for_update().get(key=key)
        bucket.available_at = max(bucket.available_at, until)
        bucket.save(update_fields=["available_at"])


def defer_delivery(application, until):
    if application.channel == "email":
        return defer_account(application.owner_id, until)
    from .cato_greenhouse import supported

    defer_portal(until, "cato_greenhouse" if supported(application) else "secret_telaviv")


def portal_pause_seconds(provider="secret_telaviv"):
    key = hashlib.sha256(f"cooldown:{provider}".encode()).hexdigest()
    bucket = DeliveryThrottle.objects.filter(key=key).first()
    return max(0, (bucket.available_at - timezone.now()).total_seconds()) if bucket else 0


@transaction.atomic
def defer_portal(until, provider="secret_telaviv"):
    keys = sorted(
        hashlib.sha256(value.encode()).hexdigest()
        for value in (f"provider:{provider}", f"cooldown:{provider}")
    )
    for key in keys:
        DeliveryThrottle.objects.get_or_create(key=key)
        bucket = DeliveryThrottle.objects.select_for_update().get(key=key)
        bucket.available_at = max(bucket.available_at, until)
        bucket.save(update_fields=["available_at"])
