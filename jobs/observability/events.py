"""Metadata-only phase events. Never serialize workflow arguments or results."""

import logging
import time
from functools import wraps

from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


def phase(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            start = time.monotonic()
            logger.info(name + "_started")
            try:
                result = function(*args, **kwargs)
            except ValidationError:
                logger.info(
                    name + "_finished",
                    extra={"outcome": "rejected", "duration_seconds": time.monotonic() - start},
                )
                raise
            except BaseException:
                logger.error(
                    name + "_finished",
                    extra={"outcome": "failed", "duration_seconds": time.monotonic() - start},
                )
                raise
            logger.info(
                name + "_finished",
                extra={"outcome": "returned", "duration_seconds": time.monotonic() - start},
            )
            return result

        return wrapped

    return decorate
