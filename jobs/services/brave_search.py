"""Fixed-destination, bounded Brave search client. Never follow authenticated redirects."""

import json
from functools import partial

import httpx
from django.conf import settings

from jobs.observability.http import payload as observe_payload
from jobs.observability.http import stream

from .crawl import cooldown, request_slot
from .network import HTTPStatusError
from .retries import retry_after_seconds

observed_stream = partial(stream, provider="brave")

HOST = "api.search.brave.com"
ENDPOINT = f"https://{HOST}/res/v1/web/search"
MAX_BYTES = 2 * 1024 * 1024


class SearchConfigurationError(ValueError):
    pass


def rate_pause(headers, fallback=1):
    pause = retry_after_seconds(headers.get("Retry-After"), fallback)
    remaining = headers.get("X-RateLimit-Remaining", "").split(",")
    resets = headers.get("X-RateLimit-Reset", "").split(",")
    for left, reset in zip(remaining, resets, strict=False):
        if left.strip() == "0":
            pause = max(pause, retry_after_seconds(reset.strip(), fallback))
    return pause


def search(query: str, offset: int) -> tuple[list[str], bool]:
    key = settings.BRAVE_SEARCH_API_KEY
    if not key:
        raise SearchConfigurationError("Configure BRAVE_SEARCH_API_KEY")
    if not isinstance(query, str) or not 1 <= len(query) <= 400 or not 0 <= offset <= 9:
        raise ValueError("Invalid search request")
    with (
        request_slot(HOST),
        httpx.Client(
            timeout=httpx.Timeout(20, connect=10), follow_redirects=False, trust_env=False
        ) as client,
    ):
        with observed_stream(
            client,
            "GET",
            ENDPOINT,
            params={"q": query, "count": 20, "offset": offset, "safesearch": "moderate"},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        ) as response:
            if response.status_code != 200:
                if response.status_code in {429, 503}:
                    cooldown(HOST, rate_pause(response.headers, 60))
                raise HTTPStatusError(response.status_code, str(rate_pause(response.headers, 60)))
            body = bytearray()
            for chunk in response.iter_bytes():
                observe_payload("brave", len(chunk))
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ValueError("Search response exceeds limit")
            # Respect every exhausted quota window, including monthly windows.
            cooldown(HOST, rate_pause(response.headers))
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Malformed search response")
    web = payload.get("web", {})
    query_data = payload.get("query", {})
    if not isinstance(web, dict) or not isinstance(query_data, dict):
        raise ValueError("Malformed search response")
    rows = web.get("results", [])
    more = query_data.get("more_results_available", False)
    if not isinstance(rows, list) or len(rows) > 20 or type(more) is not bool:
        raise ValueError("Malformed search results")
    if any(not isinstance(row, dict) or not isinstance(row.get("url"), str) for row in rows):
        raise ValueError("Malformed search result URL")
    return [row["url"] for row in rows], more
