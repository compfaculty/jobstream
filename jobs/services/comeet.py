"""Read-only hosted Comeet inventories. Embedded HTML never proves completeness."""

import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .network import canonical_url
from .normalization import text_blocks

HOSTS = {"www.comeet.com", "comeet.com", "www.comeet.co", "comeet.co"}
BOARD = "https://www.comeet.com/jobs/akeyless/27.006"


def identity(url: str) -> tuple[str, str, str]:
    parts = urlsplit(canonical_url(url))
    match = re.fullmatch(
        r"/jobs/([a-z0-9-]+)/([A-Z0-9]+\.[A-Z0-9]+)(?:/([a-z0-9_-]+)/([A-Z0-9]+\.[A-Z0-9]+(?:-[A-Z0-9]+\.[A-Z0-9]+)?))?/?",
        parts.path,
    )
    if parts.hostname not in HOSTS or not match or parts.query or parts.fragment:
        raise ValueError("Expected a public Comeet board or vacancy URL")
    return match[1], match[2], match[4] or ""


def embedded(data: bytes, name: str):
    """Decode JSON data only; never evaluate a provider's JavaScript."""
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("Comeet response exceeds limit")
    values = []
    for script in BeautifulSoup(data, "html.parser").find_all("script", limit=500):
        text = script.string or ""
        for match in re.finditer(r"\b" + re.escape(name) + r"\s*=\s*", text):
            try:
                value, end = json.JSONDecoder().raw_decode(text[match.end() :])
            except (ValueError, RecursionError) as exc:
                raise ValueError("Malformed Comeet embedded data") from exc
            if not text[match.end() + end :].lstrip().startswith(";"):
                raise ValueError("Unexpected Comeet data expression")
            values.append(value)
    if len(values) != 1:
        raise ValueError("Missing or ambiguous Comeet embedded data")
    return values[0]


class HostedAdapter:
    def scope(self, url):
        slug, company, posting = identity(url)
        if posting:
            raise ValueError("Configure a Comeet board, not a vacancy")
        return f"comeet:{company}:"

    def complete(self, data, base_url, limit):
        # No independently verifiable total or pagination contract in this HTML.
        return False

    def discover(self, data, base_url, limit):
        from .sources import Listing, plain

        slug, company, _ = identity(base_url)
        scope = self.scope(base_url)
        rows = embedded(data, "COMPANY_POSITIONS_DATA")
        if not isinstance(rows, list) or len(rows) > min(limit, 100000):
            raise ValueError("Invalid or over-budget Comeet inventory")
        seen = set()
        listings = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid Comeet posting")
            uid = row.get("uid")
            # Active pages may be external shorteners; use the provider-hosted route.
            url = row.get("url_comeet_hosted_page") or row.get("url_active_page")
            if not isinstance(uid, str) or not isinstance(url, str):
                raise ValueError("Missing Comeet posting identity")
            if identity(url) != (slug, company, uid) or uid in seen:
                raise ValueError("Mismatched or duplicate Comeet identity")
            seen.add(uid)
            fields, location = row.get("custom_fields"), row.get("location")
            # General-interest vacancies legitimately have no assigned location.
            if location is None:
                location = {"name": ""}
            if not isinstance(fields, dict) or not isinstance(location, dict):
                raise ValueError("Malformed Comeet posting details")
            details = fields.get("details")
            if (
                not isinstance(details, list)
                or not details
                or any(
                    not isinstance(d, dict)
                    or not isinstance(d.get("name"), str)
                    or (d.get("value") is not None and not isinstance(d["value"], str))
                    for d in details
                )
            ):
                raise ValueError("Missing Comeet description")
            if not isinstance(row.get("name"), str) or not isinstance(location.get("name"), str):
                raise ValueError("Invalid Comeet title or location")
            title = plain(row["name"])
            description = "\n".join(
                text_blocks(d["name"]) + "\n" + text_blocks(d["value"]) for d in details
            )
            if not title or not description.strip() or row.get("is_internal") is True:
                raise ValueError("Invalid public Comeet posting")
            listings.append(
                Listing(
                    url=url,
                    apply_url=url,
                    title=title,
                    company=plain(row.get("company_name")),
                    location=plain(location.get("name")),
                    description=description,
                    provider_key=scope + uid,
                )
            )
        return listings


def akeyless_job_url(job) -> str:
    company, source = job.company_profile, job.employer_source
    url = job.apply_url or job.canonical_url
    slug, board, uid = identity(url)
    if (
        (slug, board) != ("akeyless", "27.006")
        or not uid
        or job.provider_key != f"comeet:27.006:{uid}"
        or company is None
        or not company.website_verified
        or company.official_domain not in {"akeyless.io", "akeyless.com"}
        or source is None
        or source.company_id != company.pk
        or source.adapter != "akeyless"
        or not source.authoritative
        or source.discovery_pending
        or identity(source.url) != ("akeyless", "27.006", "")
    ):
        raise ValueError("Not a verified Akeyless Comeet vacancy")
    return canonical_url(url)


def manual_url(job) -> str:
    try:
        return akeyless_job_url(job)
    except ValueError:
        return ""
