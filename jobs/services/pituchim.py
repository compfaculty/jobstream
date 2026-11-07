"""Public HTML directory only; never call Pituchim's disallowed API endpoints."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from .crawl import cooldown
from .network import HTTPStatusError, canonical_url, fetch_public_document

BASE = "https://pituchim.com/startups"
HOST = "pituchim.com"
MAX_PAGE = 10000
MAX_COMPANIES = 60


@dataclass(frozen=True)
class DirectoryCompany:
    name: str
    website: str


def page_url(page: int) -> str:
    if type(page) is not int or not 1 <= page <= MAX_PAGE:
        raise ValueError("Invalid directory page")
    return BASE if page == 1 else f"{BASE}?page={page}"


def parse(
    data: bytes, page: int, *, hiring_only: bool = True
) -> tuple[list[DirectoryCompany], bool]:
    url = page_url(page)
    soup = BeautifulSoup(data, "html.parser")
    cards = soup.select("article.company-row")
    if not 1 <= len(cards) <= MAX_COMPANIES:
        raise ValueError("Directory company markup changed")
    companies = []
    usable = 0
    seen = set()
    for card in cards:
        anchor = card.select_one("h3 a[href]")
        if anchor is None:
            continue
        name = " ".join(anchor.get_text(" ", strip=True).split())
        if not 1 <= len(name) <= 250:
            continue
        try:
            website = canonical_url(urljoin(url, str(anchor["href"])))
        except ValueError, UnicodeError:
            continue
        # Internal links, favicons, tracking and actions are not employers.
        if len(website) > 2048 or urlsplit(website).hostname in {HOST, "www." + HOST}:
            continue
        usable += 1
        hiring = card.select_one(".company-hiring")
        if hiring_only and (hiring is None or hiring.get_text(" ", strip=True).lower() != "hiring"):
            continue
        key = (name.casefold(), website)
        if key not in seen:
            seen.add(key)
            companies.append(DirectoryCompany(name, website))
    if not usable:
        raise ValueError("No usable directory companies")
    more = False
    for anchor in soup.find_all("a", href=True, limit=1000):
        if anchor.get_text(" ", strip=True).lower() != "next":
            continue
        target = canonical_url(urljoin(url, str(anchor["href"])))
        parts = urlsplit(target)
        query = parse_qs(parts.query)
        if (
            parts.hostname != HOST
            or parts.path != "/startups"
            or query != {"page": [str(page + 1)]}
            or page >= MAX_PAGE
        ):
            raise ValueError("Unexpected directory pagination")
        more = True
    # Distinguish a genuine last page from accidentally missing pagination markup.
    status = soup.get_text(" ", strip=True)
    match = re.search(r"\bPage\s+(\d+)\s+of\s+(\d+)\b", status)
    if match is None or int(match[1]) != page or not page <= int(match[2]) <= MAX_PAGE:
        raise ValueError("Missing or inconsistent directory page count")
    if more != (page < int(match[2])):
        raise ValueError("Directory pagination is incomplete")
    return companies, more


def check_robots(page: int) -> None:
    url = page_url(page)
    try:
        body, robots_url = fetch_public_document(f"https://{HOST}/robots.txt", max_bytes=128 * 1024)
    except HTTPStatusError as exc:
        if exc.status != 404:
            raise
    else:
        if urlsplit(robots_url).hostname != HOST:
            raise ValueError("Unexpected robots redirect")
        rules = RobotFileParser()
        rules.parse(body.decode("utf-8", errors="replace").splitlines())
        if not rules.can_fetch("Jobstream/0.1", url):
            raise ValueError("Directory crawling disallowed by robots.txt")


def fetch(page: int, *, robots_approved: bool = False) -> tuple[list[DirectoryCompany], bool]:
    url = page_url(page)
    if not robots_approved:
        check_robots(page)
    data, effective = fetch_public_document(url)
    if canonical_url(effective) != canonical_url(url):
        raise ValueError("Unexpected directory redirect")
    result = parse(data, page)
    cooldown(HOST, 5)
    return result
