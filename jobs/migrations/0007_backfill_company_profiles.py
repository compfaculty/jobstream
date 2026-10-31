import hashlib
import unicodedata

from django.db import migrations
from django.db.models import OuterRef, Subquery


def backfill(apps, schema_editor):
    Job = apps.get_model("jobs", "Job")
    Origin = apps.get_model("jobs", "JobOrigin")
    Company = apps.get_model("jobs", "Company")
    alias = schema_editor.connection.alias
    origins = Origin.objects.using(alias).filter(job_id=OuterRef("pk")).order_by("pk")
    jobs = Job.objects.using(alias).filter(company_profile__isnull=True).exclude(company="")
    jobs = jobs.annotate(origin_source=Subquery(origins.values("source_id")[:1]))
    for job in jobs.iterator(chunk_size=500):
        name = " ".join(job.company.split())[:250]
        if not name:
            continue
        # No source provenance: do not silently merge unrelated employers by name.
        scope = f"source:{job.origin_source}" if job.origin_source else f"legacy-job:{job.pk}"
        normalized = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
        key = hashlib.sha256(f"{scope}:{normalized}".encode()).hexdigest()
        company, _ = Company.objects.using(alias).get_or_create(
            identity_key=key, defaults={"name": name, "source_id": job.origin_source}
        )
        Job.objects.using(alias).filter(pk=job.pk).update(company_profile_id=company.pk)


class Migration(migrations.Migration):
    dependencies = [("jobs", "0006_company_profiles")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
