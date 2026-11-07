"""Bounded public discovery feeds. Returned records contain no full job descriptions."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urlsplit

from django.utils import timezone

from .crawl import cooldown
from .employer_defaults import COUNTRIES
from .employer_evidence import location_matches
from .network import canonical_url, fetch_public_document
from .sources import plain

ARBEITNOW = "https://www.arbeitnow.com/api/job-board-api"
HIMALAYAS = "https://himalayas.app/jobs/api"
MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 1000


@dataclass(frozen=True)
class CompanyRecord:
    company_key: str
    record_key: str
    name: str
    source_url: str
    website: str = ""
    hiring: bool = False
    regional_status: str = "unknown"
    regional_evidence: str = ""
    title: str = ""
    location: str = ""
    job_url: str = ""
    application_url: str = ""
    expiry: datetime | None = None
    description: str = ""


@dataclass(frozen=True)
class FeedPage:
    records: list[CompanyRecord]
    cursor: dict
    rejected: int = 0


def text(value, maximum):
    if not isinstance(value, str):
        raise ValueError("Expected text")
    result = " ".join(plain(value).split())
    if not result or len(result) > maximum:
        raise ValueError("Invalid text length")
    return result


def public_url(value, *, preserve=False):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Invalid public URL")
    normalized = canonical_url(value)
    return value if preserve else normalized


def timestamp(value):
    if type(value) not in (int, float):
        raise ValueError("Expected timestamp")
    return datetime.fromtimestamp(value, UTC)


def endpoint(provider, cursor):
    if not isinstance(cursor, dict):
        raise ValueError("Invalid feed checkpoint")
    if provider == "arbeitnow":
        page = cursor.get("page", 1)
        if type(page) is not int or not 1 <= page <= 10000:
            raise ValueError("Invalid feed page")
        return ARBEITNOW + "?" + urlencode({"page": page})
    if provider == "himalayas":
        value = cursor.get("cursor", "")
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError("Invalid opaque cursor")
        return HIMALAYAS + "?" + urlencode({"limit": 20, **({"cursor": value} if value else {})})
    raise ValueError("Unsupported feed")


def arbeitnow_row(row):
    name = text(row.get("company_name"), 250)
    job = public_url(row.get("url"))
    parts = urlsplit(job)
    if parts.hostname not in {
        "www.arbeitnow.com",
        "www.arbeitnow.co.uk",
    } or not parts.path.startswith("/jobs/"):
        raise ValueError("Unexpected listing host")
    if parts.hostname == "www.arbeitnow.co.uk":
        return None
    job_key = text(row.get("slug"), 300)
    match = re.fullmatch(r"/jobs/companies/([a-zA-Z0-9_-]+)/[^/]+/?", parts.path)
    # Name alone never establishes identity, even within a provider.
    company_key = "company:" + match[1] if match else "listing:" + job_key
    source = f"https://www.arbeitnow.com/jobs/companies/{match[1]}" if match else job
    location = text(row.get("location"), 500) if row.get("location") else ""
    if re.search(
        r"\b(united states|united kingdom|switzerland|canada|australia|norway)\b", location, re.I
    ):
        return None
    regional = "target" if location_matches(location) else "unknown"
    return CompanyRecord(
        company_key,
        job_key,
        name,
        source,
        hiring=True,
        regional_status=regional,
        regional_evidence=(
            location
            if regional == "target"
            else f"Arbeitnow Europe feed; country unresolved: {location}"
        )[:500],
        title=text(row.get("title"), 300),
        location=location,
        job_url=job,
        application_url=job,
        description=str(row.get("description") or "")[:200000],
    )


def himalayas_row(row):
    slug = text(row.get("companySlug"), 300)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
        raise ValueError("Invalid company slug")
    job = public_url(row.get("guid"))
    if urlsplit(job).hostname != "himalayas.app":
        raise ValueError("Unexpected listing host")
    expiry = timestamp(row.get("expiryDate"))
    restrictions = row.get("locationRestrictions")
    if not isinstance(restrictions, list) or len(restrictions) > 250:
        raise ValueError("Invalid country restrictions")
    labels = []
    unknown = False
    country_names = {name.casefold(): name for name in COUNTRIES.values()}
    country_names["czech republic"] = "Czechia"
    for item in restrictions:
        # Both the documented objects and the current public feed's country strings occur.
        if isinstance(item, str):
            code, name = item.upper(), item.casefold()
        elif isinstance(item, dict):
            code = str(item.get("alpha2", "")).upper()
            name = str(item.get("name", "")).casefold()
        else:
            unknown = True
            continue
        if code in COUNTRIES:
            labels.append(COUNTRIES[code])
        elif name in country_names:
            labels.append(country_names[name])
        elif re.fullmatch(r"[A-Z]{2}", code):
            continue
        elif name in {"", "remote", "unknown", "europe", "emea", "worldwide"}:
            unknown = True
        elif isinstance(item, dict) and not re.fullmatch(r"[A-Z]{2}", code):
            unknown = True
    if restrictions and not labels and not unknown:
        return None
    regional = "target" if labels or not restrictions else "unknown"
    location = (
        ", ".join(labels) if labels else ("Worldwide" if not restrictions else "Country unresolved")
    )
    return CompanyRecord(
        "company:" + slug,
        job,
        text(row.get("companyName"), 250),
        f"https://himalayas.app/companies/{slug}",
        hiring=expiry > timezone.now(),
        regional_status=regional,
        regional_evidence=location[:500],
        title=text(row.get("title"), 300),
        location=location[:500],
        job_url=job,
        application_url=public_url(row.get("applicationLink"), preserve=True),
        expiry=expiry,
        description=str(row.get("description") or "")[:200000],
    )


def parse(provider: str, data: bytes, cursor: dict) -> FeedPage:
    if len(data) > MAX_BYTES:
        raise ValueError("Feed exceeds byte limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Expected feed object")
    rows = value.get("data" if provider == "arbeitnow" else "jobs")
    if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
        raise ValueError("Invalid feed rows")
    if provider == "arbeitnow":
        links = value.get("links")
        if not isinstance(links, dict) or "next" not in links:
            raise ValueError("Missing feed pagination")
        next_url = links["next"]
        next_cursor = {}
        if next_url is not None:
            parts = urlsplit(public_url(next_url))
            page = cursor.get("page", 1)
            if (
                parts.hostname != "www.arbeitnow.com"
                or parts.path != "/api/job-board-api"
                or parse_qs(parts.query) != {"page": [str(page + 1)]}
                or page >= 10000
            ):
                raise ValueError("Unexpected feed pagination")
            next_cursor = {"page": page + 1}
        parser = arbeitnow_row
    elif provider == "himalayas":
        next_value = value.get("nextCursor")
        if next_value is not None and (
            not isinstance(next_value, str)
            or not 1 <= len(next_value) <= 8192
            or next_value == cursor.get("cursor")
        ):
            raise ValueError("Invalid next cursor")
        seen = cursor.get("seen", [])
        if (
            not isinstance(seen, list)
            or len(seen) >= 10000
            or any(
                not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item)
                for item in seen
            )
        ):
            raise ValueError("Invalid cursor history")
        if next_value is not None:
            digest = hashlib.sha256(next_value.encode()).hexdigest()
            if digest in seen:
                raise ValueError("Repeated feed cursor")
            next_cursor = {"cursor": next_value, "seen": [*seen, digest]}
        else:
            next_cursor = {}
        parser = himalayas_row
    else:
        raise ValueError("Unknown feed provider")
    if not rows and next_cursor:
        raise ValueError("Empty nonterminal page")
    records, rejected = [], 0
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError("Expected row object")
            record = parser(row)
        except ValueError, TypeError, OverflowError, OSError:
            rejected += 1
        else:
            if record is not None:
                records.append(record)
    if rows and rejected == len(rows):
        raise ValueError("No parseable feed rows")
    return FeedPage(records, next_cursor, rejected)


def fetch(provider: str, cursor: dict) -> FeedPage:
    url = endpoint(provider, cursor)
    data, effective = fetch_public_document(url, max_bytes=MAX_BYTES)
    if canonical_url(effective) != canonical_url(url):
        raise ValueError("Unexpected feed redirect")
    result = parse(provider, data, cursor)
    cooldown(urlsplit(url).hostname, 5)
    return result
