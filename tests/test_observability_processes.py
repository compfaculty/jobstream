"""Real process checks; optional locally and required in CI's Linux test job."""

import os

import pytest

from scripts.observability_lifecycle import (
    verify_celery_prefork_retry_and_replacement,
    verify_gunicorn_worker_replacement,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("OBSERVABILITY_PROCESS_TESTS") != "1", reason="Opt-in subprocess lifecycle tests"
    ),
]


def test_gunicorn_worker_replacement(tmp_path):
    verify_gunicorn_worker_replacement(tmp_path)


def test_celery_prefork_retry_and_replacement(tmp_path):
    verify_celery_prefork_retry_and_replacement(tmp_path)
