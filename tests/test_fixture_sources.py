import json
from dataclasses import replace
from pathlib import Path

import pytest
from django.core.exceptions import PermissionDenied

from jobs.services.fixture_sources import FixtureGrant, GreenhouseFixtureProvider

PAYLOAD = (Path(__file__).parent / "fixtures/greenhouse_synthetic.json").read_bytes()
GRANT = FixtureGrant("approved", frozenset({"read_fixture"}))


@pytest.fixture
def enabled(settings):
    settings.JOB_SOURCE_FIXTURES_ENABLED = True


def test_disabled_by_default(settings):
    assert not settings.JOB_SOURCE_FIXTURES_ENABLED
    with pytest.raises(PermissionDenied):
        GreenhouseFixtureProvider(PAYLOAD).collect({}, grant=GRANT)


@pytest.mark.parametrize("status", ["proposed", "restricted", "revoked", "unknown"])
def test_permission_denied(enabled, status):
    with pytest.raises(PermissionDenied):
        GreenhouseFixtureProvider(PAYLOAD).collect({}, grant=replace(GRANT, status=status))


def test_live_permission_is_not_fixture_permission(enabled):
    with pytest.raises(PermissionDenied):
        GreenhouseFixtureProvider(PAYLOAD).collect(
            {}, grant=FixtureGrant("approved", frozenset({"ingest", "submit"}))
        )


def test_identity_provenance_and_timestamp_semantics(enabled):
    result = GreenhouseFixtureProvider(PAYLOAD).collect({}, grant=GRANT)
    assert result.complete and not result.cursor and result.expected_total == 2
    assert result.scope == "greenhouse:jobstream-fixture:"
    assert result.postings[0].provider_key == "greenhouse:jobstream-fixture:1"
    assert result.postings[0].url == "https://example.org/jobs/1"
    assert result.postings[0].posted_at is None  # updated_at is not publication time


def test_pagination_drift_and_revocation(enabled):
    value = json.loads(PAYLOAD)
    template = value["jobs"][0]
    value["jobs"] = [
        {**template, "id": i, "absolute_url": f"https://example.org/jobs/{i}"}
        for i in range(1, 252)
    ]
    value["meta"]["total"] = 251
    provider = GreenhouseFixtureProvider(json.dumps(value).encode())
    first = provider.collect({}, grant=GRANT)
    assert len(first.postings) == 250 and not first.complete
    final = provider.collect(first.cursor, grant=GRANT)
    assert len(final.postings) == 1 and final.complete and not final.cursor
    with pytest.raises(PermissionDenied):
        provider.collect(first.cursor, grant=replace(GRANT, status="revoked"))
    value["jobs"][0]["title"] = "Changed"
    with pytest.raises(ValueError, match="changed"):
        GreenhouseFixtureProvider(json.dumps(value).encode()).collect(first.cursor, grant=GRANT)


def test_empty_inventory(enabled):
    result = GreenhouseFixtureProvider(b'{"jobs": [], "meta": {"total": 0}}').collect(
        {}, grant=GRANT
    )
    assert result.complete and result.expected_total == 0


@pytest.mark.parametrize(
    "payload",
    [b"invalid", b"[]", b'{"jobs": []}', b"x" * (2 * 1024 * 1024 + 1)],
)
def test_invalid_payload(enabled, payload):
    with pytest.raises(ValueError):
        GreenhouseFixtureProvider(payload).collect({}, grant=GRANT)


@pytest.mark.parametrize("mutation", ["duplicate", "boolean_id", "live_url", "total", "title"])
def test_malformed_rows_cannot_establish_completeness(enabled, mutation):
    value = json.loads(PAYLOAD)
    if mutation == "duplicate":
        value["jobs"][1] = value["jobs"][0]
    elif mutation == "boolean_id":
        value["jobs"][0]["id"] = True
    elif mutation == "live_url":
        value["jobs"][0]["absolute_url"] = "https://employer.invalid/jobs/1"
    elif mutation == "total":
        value["meta"]["total"] = 3
    else:
        value["jobs"][0]["title"] = ""
    with pytest.raises(ValueError):
        GreenhouseFixtureProvider(json.dumps(value).encode()).collect({}, grant=GRANT)


@pytest.mark.parametrize(
    "cursor", [None, {"offset": True}, {"unknown": 1}, {"offset": -1, "payload_hash": "x"}]
)
def test_invalid_checkpoint(enabled, cursor):
    with pytest.raises(ValueError):
        GreenhouseFixtureProvider(PAYLOAD).collect(cursor, grant=GRANT)


def test_item_cap_and_malformed_location(enabled):
    value = json.loads(PAYLOAD)
    value["jobs"][0]["location"] = ["unexpected"]
    with pytest.raises(ValueError, match="fields"):
        GreenhouseFixtureProvider(json.dumps(value).encode()).collect({}, grant=GRANT)
    value = json.loads(PAYLOAD)
    template = value["jobs"][0]
    value["jobs"] = [
        {**template, "id": i, "absolute_url": f"https://example.org/jobs/{i}"}
        for i in range(1, 1002)
    ]
    value["meta"]["total"] = len(value["jobs"])
    with pytest.raises(ValueError, match="bounded"):
        GreenhouseFixtureProvider(json.dumps(value).encode()).collect({}, grant=GRANT)
