"""Bounded collection batches with explicit inventory-completeness evidence."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.utils import timezone

from .comeet import HostedAdapter
from .crawl import CrawlDeferred
from .network import HTTPStatusError, canonical_url, fetch_public, request_public_document
from .normalization import text_blocks
from .retries import retry_after_seconds
from .sources import ADAPTERS, Listing, plain, posted


@dataclass(frozen=True)
class CollectionResult:
    postings: list[Listing]
    scope: str
    complete: bool = False
    cursor: dict = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    expected_total: int | None = None
    retry_at: datetime | None = None


def workday_site(url: str) -> tuple[str, str, str]:
    parts = urlsplit(canonical_url(url))
    match = re.fullmatch(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", parts.hostname or "")
    if not parts.hostname or not match or parts.query:
        raise ValueError("Configure an unfiltered Workday career site URL")
    path = [part for part in parts.path.split("/") if part]
    if path and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", path[0]):
        path.pop(0)
    if not path or not re.fullmatch(r"[A-Za-z0-9_-]+", path[0]):
        raise ValueError("Missing Workday career site")
    return parts.hostname, match[1], path[0]


class WorkdayAdapter:
    """One search page and up to ten detail GETs per invocation; cursor commits atomically."""

    def collect(self, source, cursor, *, request=None):
        request = request or request_public_document
        host, tenant, site = workday_site(source.url)
        base = f"https://{host}/wday/cxs/{tenant}/{site}"
        scope = f"workday:{tenant}:{site}"
        offset = cursor.get("offset", 0)
        if not isinstance(offset, int) or offset < 0 or offset > 100000:
            raise ValueError("Invalid Workday cursor")
        pending = cursor.get("pending")
        if pending is not None:
            total = cursor.get("total")
            next_offset = cursor.get("next_offset")
            if (
                not isinstance(pending, list)
                or not 1 <= len(pending) <= 10
                or type(total) is not int
                or type(next_offset) is not int
                or not offset < next_offset <= total <= 100000
            ):
                raise ValueError("Invalid Workday detail checkpoint")
            paths = list(pending)
        else:
            data = json.loads(
                request(
                    base + "/jobs",
                    max_bytes=source.max_response_bytes,
                    payload={"appliedFacets": {}, "limit": 10, "offset": offset, "searchText": ""},
                )[0]
            )
            total, rows = data.get("total"), data.get("jobPostings")
            if type(total) is not int or total < 0 or not isinstance(rows, list) or len(rows) > 10:
                raise ValueError("Malformed Workday inventory")
            if total > 100000 or offset + len(rows) > total or (not rows and offset < total):
                raise ValueError("Invalid Workday pagination")
            if cursor and cursor.get("total") != total:
                raise ValueError("Workday inventory changed during pagination")
            next_offset = offset + len(rows)
            paths = [row.get("externalPath") if isinstance(row, dict) else None for row in rows]
        listings = []
        for index, path in enumerate(paths):
            if (
                not isinstance(path, str)
                or not path.startswith("/job/")
                or any(part in {".", ".."} for part in path.split("/"))
                or "?" in path
                or "#" in path
            ):
                raise ValueError("Invalid Workday detail path")
            try:
                detail = json.loads(request(base + path, max_bytes=source.max_response_bytes)[0])
            except (CrawlDeferred, HTTPStatusError) as exc:
                if isinstance(exc, HTTPStatusError) and exc.status not in {429, 503}:
                    raise
                until = (
                    exc.until
                    if isinstance(exc, CrawlDeferred)
                    else (
                        timezone.now() + timedelta(seconds=retry_after_seconds(exc.retry_after, 60))
                    )
                )
                return CollectionResult(
                    postings=listings,
                    scope=scope,
                    expected_total=total,
                    retry_at=until,
                    cursor={
                        "offset": offset,
                        "total": total,
                        "pending": paths[index:],
                        "next_offset": next_offset,
                    },
                )
            info = detail.get("jobPostingInfo")
            if not isinstance(info, dict):
                raise ValueError("Missing Workday job details")
            req = info.get("jobReqId")
            if not isinstance(req, str) or not re.fullmatch(r"[\w-]{1,120}", req):
                raise ValueError("Missing Workday requisition identity")
            # Never follow an application URL returned by an untrusted payload.
            url = f"https://{host}/{site}{path}"
            locations = [info.get("location", "")]
            additional = info.get("additionalLocations", [])
            if isinstance(additional, list):
                locations.extend(x for x in additional if isinstance(x, str))
            listings.append(
                Listing(
                    url=url,
                    title=plain(info.get("title")),
                    description=text_blocks(info.get("jobDescription", "")),
                    location=", ".join(filter(None, locations)),
                    apply_url=url,
                    provider_key=f"workday:{tenant}:{req}",
                    posted_at=posted(info.get("startDate")),
                )
            )
        return CollectionResult(
            postings=listings,
            scope=scope,
            complete=next_offset == total,
            cursor={} if next_offset == total else {"offset": next_offset, "total": total},
            expected_total=total,
        )


def source_adapter(source):
    adapter = ADAPTERS.get(source.adapter)
    if source.adapter == "akeyless" and urlsplit(source.url).hostname in {
        "www.comeet.com",
        "comeet.com",
        "www.comeet.co",
        "comeet.co",
    }:
        adapter = HostedAdapter()
    return adapter


def collect(source, cursor, *, fetch=fetch_public):
    if source.adapter == "workday":
        return WorkdayAdapter().collect(source, cursor), b""
    adapter = source_adapter(source)
    data = (
        fetch(source.url)
        if source.max_response_bytes == 2 * 1024 * 1024
        else fetch(source.url, max_bytes=source.max_response_bytes)
    )
    limit = min(source.max_items, 100000)
    postings = adapter.discover(data, source.url, limit)
    complete = hasattr(type(adapter), "complete") and adapter.complete(data, source.url, limit)
    scope = (
        adapter.scope(source.url)
        if hasattr(type(adapter), "scope")
        else "url:" + hashlib.sha256(source.url.encode()).hexdigest()
    )
    # Public full-response APIs still need bounded database/normalization batches.
    if hasattr(type(adapter), "complete"):
        # Hosted HTML contains volatile widget/session markup unrelated to jobs.
        snapshot = (
            json.dumps([asdict(posting) for posting in postings], sort_keys=True).encode()
            if isinstance(adapter, HostedAdapter)
            else data
        )
        digest = hashlib.sha256(snapshot).hexdigest()
        offset = cursor.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(postings):
            raise ValueError("Invalid inventory offset")
        if cursor and cursor.get("payload_hash") != digest:
            raise ValueError("Inventory changed during pagination")
        batch = postings[offset : offset + 250]
        next_offset = offset + len(batch)
        done = next_offset == len(postings)
        return CollectionResult(
            postings=batch,
            scope=scope,
            complete=bool(complete and done),
            cursor={} if done else {"offset": next_offset, "payload_hash": digest},
            expected_total=len(postings) if complete else None,
        ), data
    return CollectionResult(postings=postings, scope=scope, complete=complete), data
