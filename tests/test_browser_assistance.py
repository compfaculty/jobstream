from unittest.mock import Mock

import pytest

from jobs.services import browser_assistance as assistance
from jobs.services.secret_telaviv import PortalRejected


def test_human_handoff_preserves_page_and_does_not_submit():
    page, callback = Mock(), Mock()
    page.locator.return_value.evaluate_all.return_value = [["email", "synthetic@example.invalid"]]
    challenge = Mock(side_effect=[True, True, False])
    assistance.wait_for_human(page, challenge, callback, deadline=float("inf"), provider="fixture")
    callback.assert_called_once_with(page)
    page.wait_for_timeout.assert_called_once_with(100)
    page.close.assert_not_called()


def test_challenge_requires_human_callback():
    with pytest.raises(PortalRejected, match="human_verification_required"):
        assistance.wait_for_human(Mock(), lambda page: True, None, deadline=0, provider="fixture")


def test_changed_applicant_fields_require_new_review():
    page = Mock()
    page.locator.return_value.evaluate_all.side_effect = [[["original"]], [["changed"]]]
    with pytest.raises(PortalRejected, match="reviewed_content_changed"):
        assistance.wait_for_human(
            page, Mock(side_effect=[True, False]), Mock(), deadline=float("inf"), provider="fixture"
        )


def test_expired_session_never_invokes_handoff():
    callback = Mock()
    with pytest.raises(PortalRejected, match="session_timeout"):
        assistance.wait_for_human(Mock(), lambda p: True, callback, deadline=0, provider="fixture")
    callback.assert_not_called()


def test_challenge_that_never_finishes_expires(monkeypatch):
    monkeypatch.setattr(assistance.time, "monotonic", Mock(side_effect=[0, 0, 2]))
    with pytest.raises(PortalRejected, match="session_timeout"):
        assistance.wait_for_human(Mock(), lambda p: True, Mock(), deadline=1, provider="fixture")


def test_cancellation_propagates_without_replaying():
    with pytest.raises(KeyboardInterrupt):
        assistance.wait_for_human(
            Mock(),
            lambda p: True,
            Mock(side_effect=KeyboardInterrupt),
            deadline=float("inf"),
            provider="fixture",
        )
