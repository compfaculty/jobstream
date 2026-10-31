"""Parse provider Retry-After values without early retries or datetime overflow."""

import math
from email.utils import parsedate_to_datetime

from django.utils import timezone


class RetryWindowTooLong(ValueError):
    pass


def retry_after_seconds(value: str, fallback: float, *, now=None) -> float:
    now = now or timezone.now()
    try:
        requested = float(value)
        if not math.isfinite(requested) or requested < 0:
            return fallback
    except ValueError, TypeError, OverflowError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return fallback
            requested = (date - now).total_seconds()
        except ValueError, TypeError, OverflowError:
            return fallback
    if requested > 365 * 86400:
        raise RetryWindowTooLong("Provider pause exceeds the automatic retry horizon")
    return max(fallback, requested)
