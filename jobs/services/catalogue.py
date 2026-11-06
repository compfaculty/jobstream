"""Operator-attested company identities and evidence-backed career discovery."""

import csv
import hashlib
import unicodedata
import uuid
from typing import TextIO
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from django.db import connection, transaction

from jobs.models import Company, CompanyAlias, Job, Source

from .crawl import collection_context
from .discovery import ats_endpoint, propose
from .network import canonical_url, fetch_public_document


def domain(url: str) -> str:
    host = urlsplit(canonical_url(url)).hostname
    if host is None:
        raise ValueError("Missing company hostname")
    return host.removeprefix("www.")


def retain_identity(company: Company, identity: str, name: str) -> None:
    if CompanyAlias.objects.filter(legacy_identity=identity).exists():
        return
    alias = CompanyAlias.objects.filter(company=company, name=name).first()
    if alias and alias.legacy_identity is None:
        alias.legacy_identity = identity
        alias.save(update_fields=["legacy_identity"])
    else:
        label = f"{name[:220]} [{identity[:12]}]" if alias else name
        CompanyAlias.objects.create(company=company, name=label, legacy_identity=identity)


@transaction.atomic
def verify_company(company: Company) -> Company:
    """Only call after an operator attests the official website; never from CSV import."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(720316002)")
    company = Company.objects.select_for_update().get(pk=company.pk)
    website = canonical_url(company.website)
    host = domain(website)
    target = Company.objects.filter(official_domain=host).exclude(pk=company.pk).first()
    if target:
        for alias in company.aliases.all():
            name = alias.name
            if CompanyAlias.objects.filter(company=target, name=name).exists():
                name = f"{name[:210]} [{alias.pk}]"
            alias.company = target
            alias.name = name
            alias.save(update_fields=["company", "name"])
        retain_identity(target, company.identity_key, company.name)
        Source.objects.filter(company=company).update(company=target)
        Job.objects.filter(company_profile=company).update(company_profile=target)
        company.delete()
        return target
    old_key = company.identity_key
    company.identity_key = hashlib.sha256(f"domain:{host}".encode()).hexdigest()
    company.website = website
    company.website_verified = True
    company.official_domain = host
    company.review_required = False
    company.save()
    if old_key != company.identity_key:
        retain_identity(company, old_key, company.name)
    return company


@transaction.atomic
def candidate(company: Company, url: str, *, evidence: str = "", trusted: bool = False) -> Source:
    endpoint = ats_endpoint(url)
    if endpoint:
        source = propose(url, company.name, evidence or company.website, company.source)
        if source.company_id not in (None, company.pk):
            # A shared ATS board is ambiguous; do not transfer its identity or authority.
            company.review_required = True
            company.save(update_fields=["review_required"])
            return source
        source.company = company
        if trusted and endpoint[0] in {"greenhouse", "ashby", "workday"}:
            if not company.website_verified or not company.official_domain:
                raise ValueError("Automatic endpoint activation requires verified company identity")
            if source.review_reason == "Operator disabled automatic activation":
                source.save(update_fields=["company"])
                return source
            source.employer_name = company.name
            source.enabled = source.authoritative = True
            source.discovery_pending = False
            source.review_reason = ""
            source.max_items = 100000
            source.max_response_bytes = 16 * 1024 * 1024
            source.discovery_url = evidence
        source.save()
        return source
    url = canonical_url(url)
    lock = int.from_bytes(hashlib.sha256(url.encode()).digest()[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock])
    digest = hashlib.sha256(url.encode()).hexdigest()
    source, _ = Source.objects.get_or_create(
        company=company,
        url=url,
        defaults={
            "name": f"Careers: {company.name[:90]} [{company.pk}:{digest[:12]}]",
            "adapter": "jsonld",
            "employer_name": company.name,
            "discovery_pending": True,
            "review_reason": "Inspect HTML/JSON-LD or add an adapter",
            "discovery_url": evidence,
            "interval_minutes": 1440,
        },
    )
    return source


def import_companies(stream: TextIO) -> tuple[int, int]:
    reader = csv.DictReader(stream)
    if not reader.fieldnames or not {"name", "website"}.issubset(reader.fieldnames):
        raise ValueError("CSV requires name,website; career_url is optional")
    created = existing = 0
    # Validate every row before writing anything; imports are all-or-nothing.
    rows = []
    for number, row in enumerate(reader, 2):
        name = " ".join((row.get("name") or "").split())
        if not name or len(name) > 250:
            raise ValueError(f"Invalid company name at row {number}")
        website = canonical_url(row.get("website") or "")
        career = canonical_url(row["career_url"]) if row.get("career_url") else ""
        if max(len(website), len(career)) > 2048:
            raise ValueError(f"URL exceeds limit at row {number}")
        rows.append((name, website, career))
        if len(rows) > 100000:
            raise ValueError("Import exceeds 100000 rows")
    with transaction.atomic():
        for name, website, career in rows:
            normalized_name = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
            key = hashlib.sha256(
                f"catalogue:{domain(website)}:{normalized_name}".encode()
            ).hexdigest()
            company = Company.objects.filter(official_domain=domain(website)).first()
            added = False
            if company is None:
                company, added = Company.objects.get_or_create(
                    identity_key=key, defaults={"name": name, "website": website}
                )
            created += int(added)
            existing += int(not added)
            if company.name != name:
                CompanyAlias.objects.get_or_create(company=company, name=name)
            if career:
                candidate(company, career)  # CSV URLs are proposals, never evidence of authority.
    return created, existing


def discover_company(company: Company) -> list[Source]:
    if not company.website_verified or not company.official_domain:
        raise ValueError("Verify official company identity first")
    proposals = []
    with collection_context():
        data, effective = fetch_public_document(company.website)
        if domain(effective) != company.official_domain:
            raise ValueError("Official website redirected to a different domain; review required")
        queue = [(data, effective)]
        visited = {effective}
        # Inspect homepage plus at most four directly linked careers pages.
        for body, page in queue:
            soup = BeautifulSoup(body, "html.parser")
            for anchor in soup.find_all(["a", "iframe"], limit=500):
                href = anchor.get("href") or anchor.get("src")
                if not isinstance(href, str):
                    continue
                try:
                    url = canonical_url(urljoin(page, href))
                except ValueError:
                    continue
                if ats_endpoint(url):
                    proposals.append(candidate(company, url, evidence=page, trusted=True))
                    if len(proposals) >= 20:
                        return proposals
                elif any(
                    word in (urlsplit(url).path + " " + anchor.get_text(" ", strip=True)).lower()
                    for word in ("career", "vacanc", "jobs", "join-us", "positions")
                ):
                    if url not in visited and len(proposals) < 20:
                        proposals.append(candidate(company, url, evidence=page))
                        if domain(url) != company.official_domain or len(visited) >= 5:
                            continue
                        visited.add(url)
                        # Inspect known career pages, never crawl the whole website.
                        child, effective_child = fetch_public_document(url)
                        if ats_endpoint(effective_child):
                            proposals.append(
                                candidate(company, effective_child, evidence=page, trusted=True)
                            )
                        elif domain(effective_child) == company.official_domain:
                            queue.append((child, effective_child))
    return proposals


def new_identity():
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()
