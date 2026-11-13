"""Offline M1 contract exercise; deliberately absent from the adapter/task registry.

A fixture grant authorizes synthetic bytes only. It is never a live-source grant.
Reuse the production collector's bounded pagination and identity conventions without
accepting URLs, credentials, paths, or a network transport from callers.
"""

import json
from dataclasses import dataclass
from types import SimpleNamespace

from django.conf import settings
from django.core.exceptions import PermissionDenied

from .inventories import CollectionResult, collect
from .sources import normalize

BOARD = "jobstream-fixture"
ENDPOINT = f"https://boards-api.greenhouse.io/v1/boards/{BOARD}/jobs?content=true"
MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 1000


@dataclass(frozen=True)
class FixtureGrant:
    status: str = "proposed"
    allowed_uses: frozenset[str] = frozenset()


@dataclass(frozen=True)
class GreenhouseFixtureProvider:
    payload: bytes

    def collect(self, cursor: dict, *, grant: FixtureGrant) -> CollectionResult:
        """Parse a bounded synthetic snapshot; no network or database writes.

        Each call rechecks the grant, including between pages. Callers must retain
        the existing ingestion lease/transaction boundary before persisting results.
        """
        if not settings.JOB_SOURCE_FIXTURES_ENABLED:
            raise PermissionDenied("Fixture source exercise is disabled.")
        if grant.status != "approved" or grant.allowed_uses != frozenset({"read_fixture"}):
            raise PermissionDenied("An explicit synthetic-fixture grant is required.")
        if not isinstance(self.payload, bytes) or len(self.payload) > MAX_BYTES:
            raise ValueError("Invalid fixture byte limit")
        if not isinstance(cursor, dict):
            raise ValueError("Invalid fixture cursor")
        if cursor and (
            set(cursor) != {"offset", "payload_hash"}
            or type(cursor.get("offset")) is not int
            or not isinstance(cursor.get("payload_hash"), str)
        ):
            raise ValueError("Invalid fixture checkpoint")
        value = json.loads(self.payload)
        if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
            raise ValueError("Expected fixture jobs array")
        rows = value["jobs"]
        meta = value.get("meta")
        if (
            len(rows) > MAX_ITEMS
            or not isinstance(meta, dict)
            or type(meta.get("total")) is not int
            or meta["total"] != len(rows)
        ):
            raise ValueError("Fixture must be a complete bounded snapshot")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
                raise ValueError("Invalid fixture posting ID")
            if row["id"] in seen:
                raise ValueError("Duplicate fixture posting ID")
            seen.add(row["id"])
            location = row.get("location") or {}
            if (
                not isinstance(row.get("title"), str)
                or not row["title"].strip()
                or not isinstance(location, dict)
                or not isinstance(location.get("name", ""), str)
                or not isinstance(row.get("content", ""), str)
            ):
                raise ValueError("Invalid fixture posting fields")
            # Restrict links to synthetic targets; never fetch returned URLs.
            if row.get("absolute_url") != f"https://example.org/jobs/{row['id']}":
                raise ValueError("Fixture links must use the synthetic target")
        source = SimpleNamespace(
            adapter="greenhouse", url=ENDPOINT, max_items=MAX_ITEMS, max_response_bytes=MAX_BYTES
        )
        result, _ = collect(source, cursor, fetch=lambda _url: self.payload)
        for posting in result.postings:
            normalize(posting)
        return result
