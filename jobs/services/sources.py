"""Adapters consume bounded bytes; they cannot execute administrator-supplied code."""

import hashlib
import json
import re
from calendar import timegm
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
from bs4 import BeautifulSoup
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .network import canonical_url
from .normalization import normalize_description, text_blocks


@dataclass(frozen=True)
class Listing:
    url: str
    title: str
    company: str = ""
    location: str = ""
    description: str = ""
    apply_url: str = ""
    posted_at: datetime | None = None
    provider_key: str = ""
    employer_status: str = "open"


def plain(value) -> str:
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)


def posted(value) -> datetime | None:
    parsed = parse_datetime(str(value or ""))
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


class SourceAdapter(Protocol):
    def discover(self, data: bytes, base_url: str, limit: int) -> list[Listing]: ...


class RSSAdapter:
    def discover(self, data: bytes, base_url: str, limit: int) -> list[Listing]:
        feed = feedparser.parse(data)
        if feed.bozo and not feed.entries:
            raise ValueError("Malformed feed")
        listings = []
        for entry in feed.entries[:limit]:
            if not entry.get("link") or not entry.get("title"):
                continue
            listings.append(
                Listing(
                    url=urljoin(base_url, entry.link),
                    title=plain(entry.title),
                    company=plain(entry.get("job_listing_company", entry.get("company", ""))),
                    location=plain(entry.get("job_listing_location", entry.get("location", ""))),
                    description=text_blocks(
                        (entry.get("content") or [{}])[0].get("value") or entry.get("summary", "")
                    ),
                    posted_at=self.publication_date(entry),
                )
            )
        return listings

    @staticmethod
    def publication_date(entry) -> datetime | None:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed:
            try:
                return datetime.fromtimestamp(timegm(parsed), UTC)
            except ValueError, OverflowError, TypeError:
                return None
        return posted(entry.get("published") or entry.get("updated"))


class JSONLDAdapter:
    def discover(self, data: bytes, base_url: str, limit: int) -> list[Listing]:
        soup = BeautifulSoup(data, "html.parser")
        listings = []
        for script in soup.find_all("script", type="application/ld+json")[:500]:
            try:
                value = json.loads(script.string or "")
            except ValueError, TypeError:
                continue
            nodes = value if isinstance(value, list) else [value]
            for node in nodes:
                if isinstance(node, dict) and "@graph" in node:
                    nodes = [*nodes, *node["@graph"]][:1000]
            for node in nodes:
                if not isinstance(node, dict) or node.get("@type") != "JobPosting":
                    continue
                organization = node.get("hiringOrganization") or {}
                location = node.get("jobLocation") or {}
                if isinstance(location, list):
                    location = location[0] if location else {}
                address = location.get("address", {}) if isinstance(location, dict) else {}
                address = address if isinstance(address, dict) else {}
                country = address.get("addressCountry", "")
                if isinstance(country, dict):
                    country = country.get("name", "")
                locality = ", ".join(
                    str(value)
                    for value in (
                        address.get("addressLocality"),
                        address.get("addressRegion"),
                        country,
                    )
                    if value
                )
                if not locality and node.get("jobLocationType") == "TELECOMMUTE":
                    locality = "Remote"
                listings.append(
                    Listing(
                        url=urljoin(base_url, node.get("url") or base_url),
                        title=plain(node.get("title")),
                        company=plain(organization.get("name", ""))
                        if isinstance(organization, dict)
                        else "",
                        location=plain(locality),
                        description=text_blocks(node.get("description")),
                        posted_at=posted(node.get("datePosted")),
                    )
                )
                if len(listings) >= limit:
                    return listings
        return listings


class ComeetAdapter:
    def discover(self, data: bytes, base_url: str, limit: int) -> list[Listing]:
        values = json.loads(data)
        if not isinstance(values, list):
            raise ValueError("Expected Comeet positions array")
        listings = []
        for item in values[:limit]:
            if not isinstance(item, dict):
                continue
            location = item.get("location") or {}
            details = item.get("details") or []
            listings.append(
                Listing(
                    url=item.get("url_active_page", ""),
                    title=plain(item.get("name")),
                    location=plain(location.get("name", "")) if isinstance(location, dict) else "",
                    description="\n".join(
                        plain(detail.get("value", ""))
                        for detail in details
                        if isinstance(detail, dict)
                    ),
                )
            )
        return listings


ADAPTERS: dict[str, SourceAdapter] = {
    "rss": RSSAdapter(),
    "jsonld": JSONLDAdapter(),
    "comeet": ComeetAdapter(),
}


def normalize(listing: Listing) -> dict:
    title = plain(listing.title)[:300]
    if not title:
        raise ValueError("Missing title")
    return {
        "canonical_url": canonical_url(listing.url),
        "title": title,
        "company": plain(listing.company)[:250],
        "location": plain(listing.location)[:250],
        "description": text_blocks(listing.description)[:100000],
        "normalized": normalize_description(text_blocks(listing.description)[:100000]),
        "apply_url": canonical_url(listing.apply_url or listing.url),
        "posted_at": listing.posted_at,
    }


class BoardAdapter:
    """JF selectors, kept explicit rather than accepting executable scraping configuration."""

    def __init__(self, selector: str):
        self.selector = selector

    def discover(self, data: bytes, base_url: str, limit: int) -> list[Listing]:
        soup = BeautifulSoup(data, "html.parser")
        seen = set()
        listings = []
        for anchor in soup.select(self.selector):
            url = urljoin(base_url, str(anchor.get("href", "")))
            title = plain(anchor.get_text())
            if not title or url in seen:
                continue
            seen.add(url)
            listings.append(Listing(url=url, title=title, location="Israel"))
            if len(listings) >= limit:
                break
        if not listings:
            raise ValueError("No public listings found; source may require browser access")
        return listings

    def metadata(self, listing: Listing, data: bytes) -> Listing:
        soup = BeautifulSoup(data, "html.parser")
        jsonld = JSONLDAdapter().discover(data, listing.url, 1)
        if jsonld:
            return jsonld[0]
        title = soup.select_one("h1, .wpjb-top-header-content h2")
        content = soup.select_one(".wpjb-text, .elementor-widget-theme-post-content")
        return replace(
            listing,
            title=plain(title.get_text()) if title else listing.title,
            description=text_blocks(content.get_text()) if content else "",
        )


ADAPTERS.update(
    {
        "secret_telaviv": BoardAdapter("div.wpjb-col-title a[href]"),
        "telfed": BoardAdapter("h3.uael-post__title a[href], .uael-post-grid__inner h3 a[href]"),
    }
)


class CompanyPageAdapter(BoardAdapter):
    """Explicit JF company profiles; links are restricted to expected hosts and paths."""

    def __init__(self, company: str, path_pattern: str, content_selector: str, comeet=False):
        super().__init__("a[href]")
        self.company = company
        self.path_pattern = re.compile(path_pattern)
        self.content_selector = content_selector
        self.comeet = comeet

    def discover(self, data: bytes, base_url: str, limit: int) -> list[Listing]:
        base_host = urlsplit(base_url).hostname
        soup = BeautifulSoup(data, "html.parser")
        found = []
        seen = set()
        for anchor in soup.select("a[href]"):
            try:
                url = canonical_url(urljoin(base_url, str(anchor.get("href", ""))))
            except ValueError:
                continue
            parts = urlsplit(url)
            same_host = parts.hostname == base_host
            comeet_host = parts.hostname in {
                "comeet.com",
                "www.comeet.com",
                "comeet.co",
                "www.comeet.co",
            }
            if not (comeet_host if self.comeet else same_host):
                continue
            if not self.path_pattern.fullmatch(parts.path) or url in seen:
                continue
            seen.add(url)
            title = plain(anchor.get_text()) or parts.path.rstrip("/").split("/")[-1].replace(
                "-", " "
            )
            found.append(Listing(url=url, title=title, company=self.company))
            if len(found) >= limit:
                break
        if not found:
            raise ValueError("No matching company job links; review source markup")
        return found

    def metadata(self, listing: Listing, data: bytes) -> Listing:
        soup = BeautifulSoup(data, "html.parser")
        structured = JSONLDAdapter().discover(data, listing.url, 1)
        if structured and structured[0].description:
            return replace(structured[0], company=structured[0].company or self.company)
        if self.comeet:
            for script in soup.find_all("script")[:500]:
                text = script.string or ""
                match = re.search(r"POSITION_DATA\s*=\s*", text)
                if match is None:
                    continue
                try:
                    value, _ = json.JSONDecoder().raw_decode(text[match.end() :])
                except ValueError:
                    continue
                if not isinstance(value, dict):
                    continue
                fields = value.get("custom_fields") or {}
                details = fields.get("details", []) if isinstance(fields, dict) else []
                description = "\n".join(
                    plain(item.get("value", "")) for item in details if isinstance(item, dict)
                )
                if description.strip():
                    location = value.get("location") or {}
                    return replace(
                        listing,
                        title=plain(value.get("name")) or listing.title,
                        company=self.company,
                        description=description,
                        location=plain(location.get("name", ""))
                        if isinstance(location, dict)
                        else "",
                    )
        title = soup.select_one("h1, .wpjb-top-header-content h2")
        sections = soup.select(self.content_selector)
        if (
            not title
            or not sections
            or not any(section.get_text(strip=True) for section in sections)
        ):
            raise ValueError("Company job metadata structure not found")
        location = soup.select_one(".career__location, .details__location, .job__location")
        return replace(
            listing,
            title=plain(title.get_text()),
            company=self.company,
            description="\n".join(section.get_text("\n", strip=True) for section in sections),
            location=plain(location.get_text()) if location else listing.location,
        )


class AgrematchAdapter:
    def discover(self, data: bytes, base_url: str, limit: int) -> list[Listing]:
        soup = BeautifulSoup(data, "html.parser")
        found = []
        for block in soup.select("div.job_pos_div_f"):
            title = block.select_one("h2.open_pos_title.sub_pos")
            if title is None or not plain(title.get_text()):
                continue
            name = plain(title.get_text())
            parts = urlsplit(base_url)
            query = [(key, value) for key, value in parse_qsl(parts.query) if key != "_jf"]
            query.append(("_jf", hashlib.sha256(name.encode()).hexdigest()[:16]))
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
            sections = block.select("div.pos_points")
            found.append(
                Listing(
                    url=url,
                    title=name,
                    company="Agrematch",
                    apply_url=base_url,
                    description="\n".join(
                        section.get_text("\n", strip=True) for section in sections
                    ),
                )
            )
            if len(found) >= limit:
                break
        if not found:
            raise ValueError("No Agrematch job blocks; review source markup")
        return found


class RealWorkRSSAdapter(RSSAdapter):
    def metadata(self, listing: Listing, data: bytes) -> Listing:
        soup = BeautifulSoup(data, "html.parser")
        structured = JSONLDAdapter().discover(data, listing.url, 1)
        result = structured[0] if structured else listing
        title = soup.select_one("h1")
        company = soup.select_one('a[href*="/companies/"]')
        content = soup.select_one(".job-description, .prose, article, main")
        apply_url = result.apply_url
        for anchor in soup.select("a[href]"):
            text = anchor.get_text(" ", strip=True)
            href = str(anchor.get("href", ""))
            if re.search(r"\bapply\b", text, re.I):
                try:
                    apply_url = canonical_url(urljoin(listing.url, href))
                except ValueError:
                    continue
                break
        return replace(
            result,
            url=listing.url,
            title=plain(title.get_text()) if title else result.title,
            company=plain(company.get_text()) if company else result.company,
            description=content.get_text("\n", strip=True) if content else result.description,
            apply_url=apply_url,
            posted_at=result.posted_at or listing.posted_at,
        )


ADAPTERS.update(
    {
        "realwork": RealWorkRSSAdapter(),
        "40seas": CompanyPageAdapter("40Seas", r"/open-positions/[^/]+/?", ".position-rich"),
        "ai21": CompanyPageAdapter(
            "AI21", r"/careers/[a-z0-9-]+/[A-Z0-9.]+/?", ".career__details-item"
        ),
        "audiocodes": CompanyPageAdapter(
            "AudioCodes", r"/careers/positions/.+", "div.section__body.sec-body"
        ),
        "akeyless": CompanyPageAdapter(
            "Akeyless", r"/jobs/.+", "div.userDesignedContent.company-description", comeet=True
        ),
        "agrematch": AgrematchAdapter(),
    }
)


# Imported after Listing is defined; ATS adapters reuse the common discovery contract.
from .ats import AshbyAdapter, GreenhouseAdapter  # noqa: E402

ADAPTERS.update({"greenhouse": GreenhouseAdapter(), "ashby": AshbyAdapter()})
