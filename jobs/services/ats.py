"""Reusable employer ATS adapters with explicit complete-snapshot evidence."""

import json
import re
from urllib.parse import parse_qs, urlsplit

from .normalization import text_blocks


def provider_identity(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io"}:
        match = re.fullmatch(r"/([^/]+)/jobs/(\d+)/?", parts.path)
        if match:
            return f"greenhouse:{match[1]}:{match[2]}"
    if host == "jobs.ashbyhq.com":
        match = re.fullmatch(r"/([^/]+)/([a-fA-F0-9-]{36})(?:/application)?/?", parts.path)
        if match:
            return f"ashby:{match[1]}:{match[2]}"
    return None


class GreenhouseAdapter:
    def scope(self, url):
        parts = urlsplit(url)
        match = re.fullmatch(r"/v1/boards/([\w-]+)/jobs/?", parts.path)
        if parts.hostname != "boards-api.greenhouse.io" or not match:
            raise ValueError("Configure a Greenhouse public jobs API endpoint")
        if parse_qs(parts.query).get("content") != ["true"]:
            raise ValueError("Greenhouse source requires content=true")
        return f"greenhouse:{match[1]}:"

    def payload(self, data):
        value = json.loads(data)
        if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
            raise ValueError("Expected Greenhouse jobs array")
        return value

    def discover(self, data, base_url, limit):
        from .sources import Listing, plain

        scope = self.scope(base_url)
        value = self.payload(data)
        listings = []
        for item in value["jobs"][:limit]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                raise ValueError("Invalid Greenhouse posting")
            location = item.get("location") or {}
            listings.append(
                Listing(
                    url=item.get("absolute_url", ""),
                    title=plain(item.get("title")),
                    location=plain(location.get("name", "")),
                    description=text_blocks(item.get("content", "")),
                    provider_key=f"{scope}{item['id']}",
                )
            )
        return listings

    def complete(self, data, base_url, limit):
        self.scope(base_url)
        value = self.payload(data)
        total = (value.get("meta") or {}).get("total")
        return isinstance(total, int) and total == len(value["jobs"]) <= limit


class AshbyAdapter:
    def scope(self, url):
        parts = urlsplit(url)
        match = re.fullmatch(r"/posting-api/job-board/([\w-]+)/?", parts.path)
        if parts.hostname != "api.ashbyhq.com" or not match:
            raise ValueError("Configure an Ashby public posting API endpoint")
        return f"ashby:{match[1]}:"

    def payload(self, data):
        value = json.loads(data)
        if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
            raise ValueError("Expected Ashby jobs array")
        return value

    def discover(self, data, base_url, limit):
        from .sources import Listing, plain, posted

        scope = self.scope(base_url)
        value = self.payload(data)
        listings = []
        for item in value["jobs"][:limit]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("Invalid Ashby posting")
            if item.get("isListed") is False:
                continue
            listings.append(
                Listing(
                    url=item.get("jobUrl", ""),
                    title=plain(item.get("title")),
                    location=plain(item.get("location", "")),
                    description=text_blocks(
                        item.get("descriptionHtml") or item.get("descriptionPlain", "")
                    ),
                    apply_url=item.get("applyUrl", ""),
                    posted_at=posted(item.get("publishedAt")),
                    provider_key=f"{scope}{item['id']}",
                )
            )
        return listings

    def complete(self, data, base_url, limit):
        self.scope(base_url)
        return len(self.payload(data)["jobs"]) <= limit
