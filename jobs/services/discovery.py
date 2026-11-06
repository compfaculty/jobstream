"""Propose disabled ATS sources from observed links; never infer employer authority."""

import hashlib
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from django.db import connection, transaction

from jobs.models import Source

from .network import canonical_url


def ats_endpoint(url: str) -> tuple[str, str] | None:
    try:
        url = canonical_url(url)
    except ValueError:
        return None
    parts = urlsplit(url)
    if parts.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        match = re.fullmatch(r"/([\w-]+)(?:/jobs/\d+)?/?", parts.path)
        if match:
            return (
                "greenhouse",
                f"https://boards-api.greenhouse.io/v1/boards/{match[1]}/jobs?content=true",
            )
    if parts.hostname == "jobs.ashbyhq.com":
        match = re.fullmatch(r"/([\w-]+)(?:/[a-fA-F0-9-]{36}(?:/application)?)?/?", parts.path)
        if match:
            return "ashby", f"https://api.ashbyhq.com/posting-api/job-board/{match[1]}"
    if re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", parts.hostname or ""):
        from .inventories import workday_site

        try:
            host, _tenant, site = workday_site(url)
        except ValueError:
            return None
        return "workday", f"https://{host}/{site}"
    from .comeet import BOARD, identity

    try:
        slug, company, _ = identity(url)
    except ValueError:
        return None
    if (slug, company) == ("akeyless", "27.006"):
        return "akeyless", BOARD
    return None


@transaction.atomic
def propose(url: str, employer: str, evidence_url: str, source: Source | None = None):
    endpoint = ats_endpoint(url)
    if not endpoint or not employer.strip():
        return None
    evidence_url = canonical_url(evidence_url)
    adapter, api_url = endpoint
    key = hashlib.sha256(api_url.encode()).hexdigest()
    # Serialize discoveries across simultaneous board ingestions without a network call.
    lock = int.from_bytes(hashlib.sha256(f"source:{key}".encode()).digest()[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock])
    existing = Source.objects.filter(url=api_url).first()
    if existing:
        return existing
    return Source.objects.create(
        name=f"Employer feed: {employer.strip()[:105]} [{key[:12]}]",
        adapter=adapter,
        url=api_url,
        employer_name=employer.strip()[:250],
        discovered_from=source,
        discovery_url=evidence_url,
        discovery_pending=True,
        enabled=False,
        authoritative=False,
        max_items=500,
        max_response_bytes=8 * 1024 * 1024,
        notes=(
            "Discovered ATS link. Verify employer identity and board relationship "
            "before enabling or marking authoritative."
        ),
    )


def from_listing(source, listing):
    for url in dict.fromkeys([listing.apply_url, listing.url]):
        if url:
            propose(url, listing.company, listing.url, source)


def from_html(data: bytes, base_url: str, employer: str, source=None):
    soup = BeautifulSoup(data, "html.parser")
    urls = []
    for node in soup.find_all(["a", "iframe"], limit=500):
        value = node.get("href") if node.name == "a" else node.get("src")
        if isinstance(value, str):
            urls.append(urljoin(base_url, value))
    # Bound writes even for a compromised or link-heavy company page.
    proposed = []
    seen = set()
    for url in urls:
        endpoint = ats_endpoint(url)
        if endpoint and endpoint not in seen:
            seen.add(endpoint)
            proposed.append(propose(url, employer, base_url, source))
            if len(proposed) == 5:
                break
    return proposed
