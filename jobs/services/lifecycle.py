"""Endpoint observations establish employer lifecycle; overrides establish presentation."""

from django.utils import timezone

from jobs.models import Job, JobEvent, JobOrigin

CONTENT_FIELDS = (
    "title",
    "company",
    "location",
    "description",
    "normalized",
    "apply_url",
    "posted_at",
)


def transition(job, status, reason, *, source=None, run=None):
    if job.lifecycle == status and job.status_reason == reason:
        return
    changed = job.lifecycle != status
    job.lifecycle = status
    job.status_reason = reason
    if changed:
        job.status_changed_at = timezone.now()
    job.active = (job.status_override or status) == "open"
    job.save()
    if changed:
        JobEvent.objects.create(job=job, status=status, reason=reason, source=source, run=run)


def reconcile(job, source, run):
    observations = JobOrigin.objects.filter(
        job=job, source__authoritative=True, authoritative_observation=True
    )
    if observations.filter(observed_status="open").exists():
        transition(job, "open", "employer_observed", source=source, run=run)
        job.verification = "open"
    elif source.closure_enabled:
        status = "filled" if observations.filter(observed_status="filled").exists() else "closed"
        transition(
            job,
            status,
            "employer_filled" if status == "filled" else "employer_absent",
            source=source,
            run=run,
        )
        job.verification = "closed"
    else:
        # Shadow mode: retain the visible state but record the candidate in this run.
        candidates = getattr(run, "_closure_candidates", set())
        if job.lifecycle == "open":
            candidates.add(job.pk)
        run._closure_candidates = candidates
        run.would_close = len(candidates)
    job.save(update_fields=["verification"])


def complete_inventory(source, token, run):
    origins = JobOrigin.objects.filter(source=source, scope=run.scope)
    now = timezone.now()
    for origin in origins.select_related("job").order_by("job_id"):
        job = Job.objects.select_for_update().get(pk=origin.job_id)
        if origin.inventory_token != token:
            origin.missing_count += 1
            if origin.missing_count >= 2 and origin.observed_status != "filled":
                origin.observed_status = "closed"
        origin.last_complete_at = now
        origin.save(update_fields=["missing_count", "observed_status", "last_complete_at"])
        if source.authoritative:
            reconcile(job, source, run)
