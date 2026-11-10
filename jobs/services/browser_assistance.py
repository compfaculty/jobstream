"""Bounded human handoff in the original isolated browser context."""

import logging
import time
from collections.abc import Callable

from playwright.sync_api import Page

from .secret_telaviv import PortalRejected

SESSION_SECONDS = 600
logger = logging.getLogger(__name__)


def wait_for_human(
    page: Page,
    challenged: Callable[[Page], bool],
    callback: Callable[[Page], None] | None,
    *,
    deadline: float,
    provider: str,
) -> None:
    """Yield only verification, rejecting applicant-field edits before submission.

    Hidden provider tokens and verification codes are deliberately excluded. This
    snapshot stays in memory and must never be written to diagnostics.
    """
    if callback is not None and time.monotonic() >= deadline:
        raise PortalRejected(f"{provider}_assisted_session_timeout")
    if not challenged(page):
        return
    if callback is None:
        raise PortalRejected(f"{provider}_human_verification_required")

    def fields():
        return page.locator(
            "input, select, textarea, [role=checkbox], [role=radio], button[aria-pressed]"
        ).evaluate_all("""nodes => nodes
            .filter(n => n.type !== 'hidden' &&
                !/captcha|security_code/i.test(n.name + ' ' + n.id))
            .map(n => [n.id, n.name, n.type, n.value, n.checked,
                n.getAttribute("aria-checked"), n.getAttribute("aria-pressed"),
                n.files ? Array.from(n.files, f => [f.name, f.size, f.lastModified]) : null])
        """)

    original = fields()
    started = time.monotonic()
    logger.info("browser_human_verification_required", extra={"provider": provider})
    callback(page)
    while challenged(page):
        if time.monotonic() >= deadline:
            raise PortalRejected(f"{provider}_assisted_session_timeout")
        page.wait_for_timeout(100)
    if time.monotonic() >= deadline:
        raise PortalRejected(f"{provider}_assisted_session_timeout")
    if fields() != original:
        raise PortalRejected(f"{provider}_reviewed_content_changed")
    logger.info(
        "browser_human_verification_completed",
        extra={"provider": provider, "duration_seconds": time.monotonic() - started},
    )
