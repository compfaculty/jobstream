"""Preserve legacy identities and references; only attested exact domains consolidate."""

import hashlib
from urllib.parse import urlsplit

from django.db import migrations


def backfill(apps, schema_editor):
    Company = apps.get_model("jobs", "Company")
    Alias = apps.get_model("jobs", "CompanyAlias")
    Source = apps.get_model("jobs", "Source")
    Job = apps.get_model("jobs", "Job")
    Origin = apps.get_model("jobs", "JobOrigin")
    Event = apps.get_model("jobs", "JobEvent")
    db = schema_editor.connection.alias
    domains = {}
    for company in Company.objects.using(db).order_by("pk").iterator():
        parts = urlsplit(company.website)
        host = (parts.hostname or "").lower().removeprefix("www.")
        verified = company.website_verified and parts.scheme == "https" and bool(host)
        if not verified:
            continue
        target = domains.get(host)
        old_key = company.identity_key
        if target:
            Job.objects.using(db).filter(company_profile_id=company.pk).update(
                company_profile_id=target
            )
            Source.objects.using(db).filter(company_id=company.pk).update(company_id=target)
            Alias.objects.using(db).get_or_create(
                legacy_identity=old_key,
                defaults={"company_id": target, "name": f"{company.name} [{company.pk}]"[:250]},
            )
            # The original source relationship is not authoritative evidence by itself.
            Company.objects.using(db).filter(pk=company.pk).delete()
        else:
            domains[host] = company.pk
            Company.objects.using(db).filter(pk=company.pk).update(
                identity_key=hashlib.sha256(f"domain:{host}".encode()).hexdigest(),
                official_domain=host,
                review_required=False,
            )
            Alias.objects.using(db).get_or_create(
                legacy_identity=old_key, defaults={"company_id": company.pk, "name": company.name}
            )
    boards = {"rss", "telfed", "realwork"}
    Source.objects.using(db).filter(adapter__in=boards).update(enabled=False)
    for source in (
        Source.objects.using(db).exclude(adapter__in=boards | {"secret_telaviv"}).iterator()
    ):
        candidates = (
            Company.objects.using(db)
            .filter(jobs__employer_source_id=source.pk)
            .values_list("pk", flat=True)
            .distinct()
        )
        ids = list(candidates[:2])
        if len(ids) == 1:
            Source.objects.using(db).filter(pk=source.pk).update(company_id=ids[0])
        if source.authoritative:
            Source.objects.using(db).filter(pk=source.pk).update(
                interval_minutes=1440, max_items=100000, max_response_bytes=16 * 1024 * 1024
            )
    for job in Job.objects.using(db).all().iterator(chunk_size=1000):
        status = "closed" if not job.active or job.verification == "closed" else "open"
        Job.objects.using(db).filter(pk=job.pk).update(lifecycle=status, active=status == "open")
        Origin.objects.using(db).filter(job_id=job.pk).update(
            observed_status=status, missing_count=2 if status == "closed" else 0
        )
        if status != "open":
            Event.objects.using(db).create(job_id=job.pk, status=status, reason="legacy_backfill")


class Migration(migrations.Migration):
    dependencies = [("jobs", "0027_company_first")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
