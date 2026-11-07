"""Import unverified companies and source observations without granting endpoint authority."""

import hashlib
import unicodedata
from dataclasses import dataclass

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from jobs.models import Company, CompanyProvenance, EmployerDiscoveryLead

from .catalogue import domain
from .company_feeds import CompanyRecord, public_url
from .employer_evidence import role_matches


@dataclass(frozen=True)
class ImportResult:
    created: bool = False
    rejected: bool = False
    unresolved: bool = False


def validate(record: CompanyRecord):
    for value, size in ((record.name, 250), (record.company_key, 512), (record.record_key, 2048)):
        if not isinstance(value, str) or not value.strip() or len(value) > size:
            raise ValueError("Invalid company identity")
    if record.regional_status not in {"target", "unknown"}:
        raise ValueError("Invalid region status")
    public_url(record.source_url)
    for value in (record.website, record.job_url, record.application_url):
        if value:
            public_url(value)
    for value, size in (
        (record.title, 300),
        (record.location, 500),
        (record.regional_evidence, 500),
    ):
        if not isinstance(value, str) or len(value) > size:
            raise ValueError("Invalid observation metadata")


@transaction.atomic
def observe(campaign, run, record: CompanyRecord) -> ImportResult:
    if campaign.provider in {"arbeitnow", "himalayas"}:
        raise ValueError("Job boards cannot populate the company catalogue")
    validate(record)
    now = timezone.now()
    key = hashlib.sha256(record.record_key.encode()).hexdigest()
    # Import and promotion use the same short identity coordination lock.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(720316015)")
    provenance = (
        CompanyProvenance.objects.select_for_update(no_key=True)
        .filter(provider=campaign.provider, external_identity=record.company_key)
        .first()
    )
    lead = (
        EmployerDiscoveryLead.objects.select_for_update().filter(campaign=campaign, key=key).first()
    )
    if lead and lead.status == "rejected":
        return ImportResult(rejected=True)
    added = False
    if provenance:
        company = provenance.company
    elif lead and lead.company_id:
        company = lead.company
    else:
        company = (
            Company.objects.filter(official_domain=domain(record.website)).first()
            if record.website
            else None
        )
        if company is None:
            normalized = " ".join(unicodedata.normalize("NFKC", record.name).casefold().split())
            identity = (
                f"catalogue:{domain(record.website)}:{normalized}"
                if record.website
                else f"provider-company:{campaign.provider}:{record.company_key}"
            )
            company, added = Company.objects.get_or_create(
                identity_key=hashlib.sha256(identity.encode()).hexdigest(),
                defaults={
                    "name": record.name,
                    "website": record.website,
                    "review_required": True,
                    "website_verified": False,
                },
            )
    host = domain(record.website) if record.website else ""
    possible = (
        Company.objects.filter(
            Q(name__iexact=record.name)
            | (Q(discovery_provenance__reported_domain=host) if host else Q(pk=company.pk))
        )
        .exclude(pk=company.pk)
        .exists()
    )
    if possible:
        CompanyProvenance.objects.filter(possible_duplicate=False).filter(
            Q(reported_name__iexact=record.name)
            | (Q(reported_domain=host) if host else Q(pk__isnull=True))
        ).exclude(company=company).update(possible_duplicate=True)
    if provenance is None:
        provenance = CompanyProvenance.objects.create(
            company=company,
            campaign=campaign,
            provider=campaign.provider,
            external_identity=record.company_key,
            source_url=record.source_url,
            reported_name=record.name,
        )
    provenance.campaign = campaign
    provenance.source_url = record.source_url
    provenance.reported_name = record.name
    provenance.reported_website = record.website
    provenance.reported_domain = host
    provenance.last_observed_at = now
    provenance.possible_duplicate = provenance.possible_duplicate or possible
    if record.regional_status == "target" or provenance.regional_status != "target":
        provenance.regional_status = record.regional_status
        provenance.regional_evidence = record.regional_evidence
    signal = record.hiring and (record.expiry is None or record.expiry > now)
    priority = (
        2 if signal and role_matches(record.title, campaign.technology_roles) else int(signal)
    )
    if signal:
        provenance.hiring_observed_at = now
    if lead is None:
        lead = EmployerDiscoveryLead(
            campaign=campaign,
            key=key,
            candidate_url=record.job_url or record.website or record.source_url,
            name=record.name,
            website=record.website,
        )
    # Verified vacancy fields and curated company data are not feed fields.
    lead.company, lead.provenance, lead.run = company, provenance, run
    lead.search_query = record.source_url[:400]
    lead.reported_title = record.title
    lead.reported_location = record.location
    lead.reported_application_url = record.application_url
    lead.reported_expiry = record.expiry
    lead.reported_at = now
    lead.hiring_signal = signal
    if lead.pk:
        lead.save(
            update_fields=[
                "company",
                "provenance",
                "run",
                "search_query",
                "reported_title",
                "reported_location",
                "reported_application_url",
                "reported_expiry",
                "reported_at",
                "hiring_signal",
            ]
        )
    else:
        lead.save()
    if provenance.source_lead_id is None or priority >= provenance.priority:
        provenance.source_lead = lead
        provenance.priority = priority
    provenance.save()
    return ImportResult(
        added, unresolved=not bool(company.website) or provenance.regional_status == "unknown"
    )
