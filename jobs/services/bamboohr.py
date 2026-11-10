"""Bounded public BambooHR inventories; no applicant API or account credentials."""

import hashlib
import json
import re
from urllib.parse import urlsplit

from .network import canonical_url
from .normalization import text_blocks
from .sources import Listing, plain


def collect(source, cursor, *, fetch):
    from .inventories import CollectionResult

    parts = urlsplit(canonical_url(source.url))
    if (
        not re.fullmatch(r"[a-z0-9-]+\.bamboohr\.com", parts.hostname or "")
        or parts.path != "/careers/list"
        or parts.query
    ):
        raise ValueError("Configure a BambooHR public careers list")
    root = f"https://{parts.hostname}/careers"

    def read(url):
        return json.loads(fetch(url, max_bytes=source.max_response_bytes))

    value = read(root + "/list")
    if not isinstance(value, dict) or not isinstance(value.get("meta"), dict):
        raise ValueError("Invalid BambooHR inventory")
    rows, total = value.get("result"), value["meta"].get("totalCount")
    if (
        not isinstance(rows, list)
        or type(total) is not int
        or total != len(rows)
        or not 0 <= total <= min(source.max_items, 100000)
    ):
        raise ValueError("Incomplete or over-budget BambooHR inventory")
    ids = []
    seen = set()
    for row in rows:
        uid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(uid, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", uid) or uid in seen:
            raise ValueError("Invalid or duplicate BambooHR posting")
        ids.append(uid)
        seen.add(uid)
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    offset = cursor.get("offset", 0)
    if (
        type(offset) is not int
        or not 0 <= offset <= total
        or (cursor and cursor.get("payload_hash") != digest)
    ):
        raise ValueError("Invalid or changed BambooHR checkpoint")
    listings = []
    for uid in ids[offset : offset + 10]:
        detail = read(f"{root}/{uid}/detail")
        result = detail.get("result") if isinstance(detail, dict) else None
        job = result.get("jobOpening") if isinstance(result, dict) else None
        url = f"{root}/{uid}"
        if (
            not isinstance(job, dict)
            or job.get("jobOpeningStatus") != "Open"
            or job.get("jobOpeningShareUrl") != url
            or not isinstance(job.get("jobOpeningName"), str)
            or not job["jobOpeningName"].strip()
            or not isinstance(job.get("description"), str)
            or not text_blocks(job["description"]).strip()
        ):
            raise ValueError("Invalid or changed BambooHR vacancy")
        location = job.get("location")
        if not isinstance(location, dict):
            raise ValueError("Invalid BambooHR location")
        listings.append(
            Listing(
                url=url,
                apply_url=url,
                title=plain(job["jobOpeningName"]),
                description=text_blocks(job["description"]),
                location=", ".join(
                    plain(location[key])
                    for key in ("city", "state", "addressCountry")
                    if location.get(key)
                ),
                provider_key=f"bamboohr:{parts.hostname}:{uid}",
            )
        )
    end = offset + len(listings)
    # List/detail reads are not atomic; do not use them to close other vacancies.
    return CollectionResult(
        postings=listings,
        scope=f"bamboohr:{parts.hostname}:",
        cursor={} if end == total else {"offset": end, "payload_hash": digest},
    )
