from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from jobs.services.retries import RetryWindowTooLong, retry_after_seconds

NOW = datetime(2026, 10, 31, tzinfo=UTC)


@pytest.mark.parametrize("header", ["", "invalid", "nan", "inf", "-10"])
def test_invalid_headers_use_backoff(header):
    assert retry_after_seconds(header, 60, now=NOW) == 60


def test_numeric_and_http_date_deadlines_are_honored():
    assert retry_after_seconds("600", 60, now=NOW) == 600
    date = format_datetime(NOW + timedelta(days=2), usegmt=True)
    assert retry_after_seconds(date, 60, now=NOW) == 2 * 86400
    assert retry_after_seconds(format_datetime(NOW - timedelta(seconds=1)), 60, now=NOW) == 60


def test_unrepresentable_pause_requires_review():
    with pytest.raises(RetryWindowTooLong):
        retry_after_seconds("1e300", 60, now=NOW)
