import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from jobs.services import (
    company_feeds,
)
from jobs.services.company_feeds import CompanyRecord

pytestmark = pytest.mark.django_db
HOME = "https://acme.example/"


def record(**kwargs):
    return CompanyRecord(
        company_key="company:acme",
        record_key="vacancy:1",
        name="Acme",
        source_url="https://www.arbeitnow.com/jobs/companies/acme",
        **kwargs,
    )


def arbeid_row(**kwargs):
    return {
        "slug": "engineer-1",
        "company_name": "Acme",
        "title": "Sales manager",
        "location": "Berlin, Germany",
        "url": "https://www.arbeitnow.com/jobs/companies/acme/engineer-1",
        **kwargs,
    }


def himalaya_row(**kwargs):
    return {
        "companySlug": "acme",
        "companyName": "Acme",
        "title": "Sales manager",
        "guid": "https://himalayas.app/companies/acme/jobs/sales-manager",
        "applicationLink": "https://himalayas.app/companies/acme/jobs/sales-manager?utm_source=api",
        "expiryDate": (timezone.now() + timedelta(days=30)).timestamp(),
        "locationRestrictions": [],
        **kwargs,
    }


def parse_arbeit(rows, next_url=None, cursor=None):
    return company_feeds.parse(
        "arbeitnow", json.dumps({"data": rows, "links": {"next": next_url}}).encode(), cursor or {}
    )


def parse_himalaya(rows, next_value=None, cursor=None):
    return company_feeds.parse(
        "himalayas", json.dumps({"jobs": rows, "nextCursor": next_value}).encode(), cursor or {}
    )


def test_arbeitnow_uses_company_profile_and_all_roles():
    page = parse_arbeit([arbeid_row()])
    assert page.records[0].company_key == "company:acme"
    assert page.records[0].title == "Sales manager" and not page.records[0].website
    assert page.records[0].regional_status == "target"


def test_arbeitnow_handles_empty_locations_and_uk_records():
    uk = arbeid_row(url="https://www.arbeitnow.co.uk/jobs/companies/acme/engineer-1")
    page = parse_arbeit([arbeid_row(location=""), uk])
    assert (
        len(page.records) == 1
        and page.records[0].regional_status == "unknown"
        and page.rejected == 0
    )


def test_no_profile_uses_listing_identity_instead_of_name():
    page = parse_arbeit([arbeid_row(url="https://www.arbeitnow.com/jobs/engineer-1")])
    assert page.records[0].company_key == "listing:engineer-1"


@pytest.mark.parametrize(
    "countries",
    [[], ["Germany"], [{"alpha2": "IL", "name": "Israel"}], ["UA"], ["Cyprus", "United States"]],
)
def test_himalayas_supported_location_shapes(countries):
    page = parse_himalaya([himalaya_row(locationRestrictions=countries)])
    assert len(page.records) == 1 and page.records[0].regional_status == "target"
    assert "utm_source=api" in page.records[0].application_url


@pytest.mark.parametrize("countries", [["United States"], [{"alpha2": "GB"}], ["Argentina"]])
def test_himalayas_excludes_explicit_outside_regions(countries):
    assert not parse_himalaya([himalaya_row(locationRestrictions=countries)]).records


def test_timezone_is_not_regional_evidence_and_expiry_is_not_closure():
    page = parse_himalaya(
        [
            himalaya_row(
                locationRestrictions=[{"bad": "country"}],
                timezoneRestrictions=["UTC+2"],
                expiryDate=1,
            )
        ]
    )
    assert page.records[0].regional_status == "unknown" and not page.records[0].hiring


def test_opaque_cursor_pagination_and_cycles():
    first = parse_himalaya([himalaya_row()], "opaque/A?token=1")
    url = company_feeds.endpoint("himalayas", first.cursor)
    assert "opaque%2FA%3Ftoken%3D1" in url
    second = parse_himalaya([himalaya_row()], "second", first.cursor)
    with pytest.raises(ValueError):
        parse_himalaya([himalaya_row()], "opaque/A?token=1", second.cursor)


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/api?page=2",
        "https://127.0.0.1/api?page=2",
        "https://www.arbeitnow.com/api/job-board-api?page=9",
        "https://www.arbeitnow.com/api/job-board-api?page=2&extra=1",
    ],
)
def test_feed_pagination_cannot_change_destination(url):
    with pytest.raises(ValueError):
        parse_arbeit([arbeid_row()], url)


def test_feed_rows_and_bytes_are_bounded():
    with pytest.raises(ValueError):
        parse_arbeit([arbeid_row()] * 1001)
    with pytest.raises(ValueError):
        company_feeds.parse("arbeitnow", b"x" * (company_feeds.MAX_BYTES + 1), {})
    with pytest.raises(ValueError):
        parse_arbeit([{"malformed": True}])
    with pytest.raises(ValueError):
        parse_arbeit([], company_feeds.ARBEITNOW + "?page=2")


def test_feed_transport_is_fixed_bounded_and_redirect_safe(monkeypatch):
    payload = json.dumps({"data": [arbeid_row()], "links": {"next": None}}).encode()
    fetch = Mock(return_value=(payload, company_feeds.ARBEITNOW + "?page=1"))
    monkeypatch.setattr(company_feeds, "fetch_public_document", fetch)
    monkeypatch.setattr(company_feeds, "cooldown", Mock())
    result = company_feeds.fetch("arbeitnow", {})
    assert result.records
    fetch.assert_called_once_with(company_feeds.ARBEITNOW + "?page=1", max_bytes=8 * 1024 * 1024)
    fetch.return_value = (payload, "https://attacker.example/")
    with pytest.raises(ValueError):
        company_feeds.fetch("arbeitnow", {})
