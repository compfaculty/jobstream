"""Find published contacts and distinguish application instructions from generic HR."""

import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from jobs.models import RecruitingContact

EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DENIED = {"privacy", "dpo", "security", "support", "noreply", "no-reply", "abuse"}


def candidates(description):
    result = []
    for match in list(EMAIL.finditer(description))[:20]:
        email = match.group().strip(".,;:")
        if email.split("@")[0].casefold() in DENIED:
            continue
        try:
            validate_email(email)
        except ValidationError:
            continue
        before = description[max(0, match.start() - 180) : match.start()]
        context = description[max(0, match.start() - 180) : match.end() + 100]
        if re.search(r"(?:only|must|exclusively).{0,40}(?:portal|website|form)", context, re.I):
            purpose = "recruiting"
        elif re.search(
            r"(?:send|email|submit)\b[^\n.!?]{0,100}\b(?:cv|resume|résumé|application)"
            r"[^\n.!?]{0,60}\b(?:to|at)[ :]*$|\bapplications?\s*:[ ]*$",
            before,
            re.I,
        ):
            purpose = "application"
        elif re.search(r"\b(?:recruit|hiring|careers|talent|hr)\b", context, re.I):
            purpose = "recruiting"
        else:
            continue
        result.append({"email": email, "purpose": purpose, "evidence": context[:1000]})
    return result


def observe(job, source, source_url, description):
    now = timezone.now()
    found = candidates(description)
    for value in found:
        RecruitingContact.objects.update_or_create(
            job=job,
            source=source,
            email=value["email"],
            defaults={
                "purpose": value["purpose"],
                "evidence": value["evidence"],
                "source_url": source_url,
                "last_seen_at": now,
            },
        )
    RecruitingContact.objects.filter(job=job, source=source).exclude(
        email__in=[value["email"] for value in found]
    ).delete()
    accepted = next((value["email"] for value in found if value["purpose"] == "application"), "")
    # Never overwrite an operator-set address; employer evidence outranks a board observation.
    if job.email_source_id == source.pk or (
        accepted and (not job.apply_email or (source.authoritative and job.email_source_id))
    ):
        job.apply_email = accepted
        job.email_source = source if accepted else None
        job.save(update_fields=["apply_email", "email_source"])
