"""Conservative hiring evidence; fetching does not grant company/source authority."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .ats import AshbyAdapter, GreenhouseAdapter
from .catalogue import domain
from .crawl import CrawlDeferred
from .discovery import ats_endpoint
from .employer_defaults import COUNTRIES
from .inventories import WorkdayAdapter
from .network import canonical_url, fetch_public_document
from .sources import JSONLDAdapter, plain


@dataclass
class Evidence:
    name: str = ""
    website: str = ""
    career_url: str = ""
    vacancy_title: str = ""
    vacancy_location: str = ""
    vacancy_url: str = ""
    relationship_url: str = ""
    observed_at: datetime | None = None
    evidence_kind: str = ""
    valid_until: datetime | None = None

    def fields(self):
        return asdict(self)


def role_matches(title: str, roles: list[str]) -> bool:
    return any(
        re.search(r"(?<!\w)" + re.escape(role.strip()) + r"(?!\w)", title, re.I) for role in roles
    )


def location_matches(value: object, *, country_code: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if re.search(
        r"\b(except|excluding|outside|not|timezone|time zone|northern ireland)\b", value, re.I
    ):
        return False
    if country_code and value.upper() in COUNTRIES:
        return True
    names = [
        *COUNTRIES.values(),
        "Czech Republic",
        "European Union",
        "EU",
        "Tel Aviv",
        "Tel-Aviv",
        "Jerusalem",
        "Haifa",
        "Herzliya",
        "Kyiv",
        "Kiev",
        "Lviv",
    ]
    return any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", value, re.I) for name in names)


def expiry(value, *, end_of_day=True):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid expiry")
    result = parse_datetime(value)
    if result is None:
        date = parse_date(value)
        if date is None:
            raise ValueError("Invalid expiry")
        result = datetime.combine(
            date, datetime.max.time() if end_of_day else datetime.min.time(), UTC
        )
    if timezone.is_naive(result):
        result = result.replace(tzinfo=UTC)
    return result


def json_nodes(soup):
    for script in soup.find_all("script", type="application/ld+json", limit=100):
        try:
            value = json.loads(script.string or "")
        except ValueError, TypeError, RecursionError:
            continue
        queue = value if isinstance(value, list) else [value]
        # One graph level, bounded independently of hostile graph nesting.
        for node in queue[:500]:
            if isinstance(node, dict):
                yield node
                graph = node.get("@graph")
                if isinstance(graph, list):
                    yield from (item for item in graph[:500] if isinstance(item, dict))


def structured_location(node):
    remote = node.get("jobLocationType") == "TELECOMMUTE"
    values = node.get("applicantLocationRequirements") if remote else node.get("jobLocation")
    values = values if isinstance(values, list) else [values]
    labels = []
    for item in values[:30]:
        if not isinstance(item, dict):
            continue
        if remote:
            name = item.get("name")
            if location_matches(name, country_code=True):
                labels.append(COUNTRIES.get(name.upper(), name))
        else:
            address = item.get("address")
            if not isinstance(address, dict):
                continue
            country = address.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            if location_matches(country, country_code=True):
                labels.append(
                    ", ".join(
                        str(v)
                        for v in (
                            address.get("addressLocality"),
                            COUNTRIES.get(str(country).upper(), country),
                        )
                        if v
                    )
                )
    return "; ".join(labels)[:500]


def links(soup, page):
    result = []
    for anchor in soup.find_all(["a", "iframe"], limit=500):
        raw = anchor.get("href") or anchor.get("src")
        if not isinstance(raw, str):
            continue
        try:
            url = canonical_url(urljoin(page, raw))
        except ValueError, UnicodeError:
            continue
        if len(url) <= 2048:
            result.append((url, anchor.get_text(" ", strip=True)[:200]))
    return result


def inspect(
    candidate_url: str, roles: list[str], *, company_hint: str = "", website_hint: str = ""
) -> tuple[Evidence, str]:
    """At most five public fetch operations; never persist jobs or invoke ingestion."""
    pages = {}
    remaining = 5

    def fetch(url, **kwargs):
        nonlocal remaining
        url = canonical_url(url)
        if url not in pages:
            if remaining <= 0:
                raise ValueError("Candidate page limit")
            remaining -= 1
            body, effective = fetch_public_document(url, **kwargs)
            pages[url] = (body, canonical_url(effective))
        return pages[url]

    def request(url, **kwargs):
        # Workday search POSTs use the existing safe transport and the same page budget.
        nonlocal remaining
        if remaining <= 0 and url not in pages:
            raise CrawlDeferred(timezone.now() + timedelta(seconds=60))
        if kwargs.get("payload") is None:
            return fetch(url, **kwargs)
        from .network import request_public_document

        if remaining <= 0:
            raise ValueError("Candidate page limit")
        remaining -= 1
        return request_public_document(url, **kwargs)

    initial, initial_url = fetch(candidate_url)
    queue = [(initial, initial_url)]
    seen: set[str] = set()
    name = plain(company_hint)[:250]
    website = canonical_url(website_hint) if website_hint else ""
    if website and domain(initial_url) != domain(website):
        return Evidence(name=name, website=website), "EmployerDomainRedirect"
    while queue and len(seen) < 5:
        body, page = queue.pop(0)
        if page in seen:
            continue
        seen.add(page)
        soup = BeautifulSoup(body, "html.parser")
        nodes = list(json_nodes(soup))
        outgoing = links(soup, page)
        # Employer identity must come from structured organization metadata.
        for node in nodes:
            organization = (
                node.get("hiringOrganization") if node.get("@type") == "JobPosting" else node
            )
            if not isinstance(organization, dict):
                continue
            if organization is node and node.get("@type") not in ("Organization", "Corporation"):
                continue
            org_name, org_url = organization.get("name"), organization.get("url")
            if not isinstance(org_name, str) or not 1 <= len(plain(org_name)) <= 250:
                continue
            try:
                org_url = canonical_url(org_url) if isinstance(org_url, str) else ""
                if not org_url or len(org_url) > 2048 or ats_endpoint(org_url):
                    continue
            except ValueError, UnicodeError:
                continue
            # External organization claims must be checked by visiting its website.
            if not website:
                name, website = plain(org_name), org_url
                if domain(page) != domain(website):
                    home_body, home_url = fetch(website)
                    if domain(home_url) != domain(website):
                        return Evidence(), "EmployerDomainRedirect"
                    queue.insert(0, (home_body, home_url))
        if website and domain(page) == domain(website):
            # Direct employer-page structured vacancies.
            for node in nodes:
                if node.get("@type") != "JobPosting":
                    continue
                org = node.get("hiringOrganization")
                if not isinstance(org, dict) or plain(org.get("name")) != name:
                    continue
                location = structured_location(node)
                try:
                    expires = expiry(node.get("validThrough"))
                except ValueError, TypeError, OverflowError:
                    continue
                status = str(
                    node.get("jobStatus", node.get("employmentStatus", node.get("status", "")))
                ).lower()
                page_closed = any(
                    term in soup.get_text(" ", strip=True).lower()
                    for term in (
                        "no longer accepting applications",
                        "position has been filled",
                        "job has expired",
                        "position is closed",
                        "job is closed",
                    )
                )
                if (
                    page_closed
                    or not location
                    or not role_matches(plain(node.get("title")), roles)
                    or (expires is not None and expires <= timezone.now())
                    or status in {"closed", "filled", "expired", "archived"}
                ):
                    continue
                if not node.get("description") or not node.get("datePosted"):
                    continue
                try:
                    date_posted = expiry(node["datePosted"], end_of_day=False)
                    if date_posted is None or date_posted > timezone.now():
                        continue
                    listing = JSONLDAdapter().discover(
                        (
                            '<script type="application/ld+json">' + json.dumps(node) + "</script>"
                        ).encode(),
                        page,
                        1,
                    )[0]
                    vacancy = canonical_url(listing.url)
                    if domain(vacancy) != domain(website) or len(vacancy) > 2048:
                        continue
                except ValueError, TypeError, IndexError:
                    continue
                return Evidence(
                    name,
                    website,
                    page,
                    listing.title[:300],
                    location,
                    vacancy,
                    page,
                    timezone.now(),
                    "jsonld",
                    expires,
                ), ""
            # An official outgoing link establishes the board relationship.
            for url, _label in outgoing:
                endpoint = ats_endpoint(url)
                if not endpoint:
                    continue
                adapter, api = endpoint
                if adapter in {"greenhouse", "ashby"}:
                    data, effective_api = fetch(api)
                    if effective_api != canonical_url(api):
                        continue
                    parser = GreenhouseAdapter() if adapter == "greenhouse" else AshbyAdapter()
                    postings = parser.discover(data, api, 1000)
                elif adapter == "workday":
                    from types import SimpleNamespace

                    result = WorkdayAdapter().collect(
                        SimpleNamespace(url=api, max_response_bytes=2 * 1024 * 1024),
                        {},
                        request=request,
                    )
                    postings = result.postings
                else:
                    continue
                for posting in postings:
                    if (
                        posting.employer_status != "open"
                        or not role_matches(posting.title, roles)
                        or not location_matches(posting.location)
                    ):
                        continue
                    vacancy = canonical_url(posting.url)
                    if len(vacancy) > 2048 or ats_endpoint(vacancy) != endpoint:
                        continue
                    return Evidence(
                        name,
                        website,
                        url,
                        posting.title[:300],
                        posting.location[:500],
                        vacancy,
                        page,
                        timezone.now(),
                        adapter,
                    ), ""
        # Visit same-site careers/job links; an external homepage is visited only from org metadata.
        for url, label in outgoing:
            if (
                domain(url) == domain(page)
                and url not in seen
                and any(
                    term in (urlsplit(url).path + " " + label).lower()
                    for term in ("career", "jobs", "vacanc", "positions")
                )
                and len(queue) < 5
                and remaining > 0
            ):
                child, effective = fetch(url)
                if domain(effective) == domain(page):
                    queue.append((child, effective))
    return Evidence(name=name, website=website), "NoConfirmedRegionalTechVacancy"
