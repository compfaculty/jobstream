import os

os.environ.setdefault("DEBUG", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://jobstream:local-only@127.0.0.1:5432/jobstream")

import pytest
from django.contrib.auth.models import User

from jobs.models import Application, GmailConnection, Job


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="alice", email="alice@example.com", password="a-long-test-password-123"
    )


@pytest.fixture
def other(db):
    return User.objects.create_user(username="bob", password="a-long-test-password-456")


@pytest.fixture
def job(db):
    return Job.objects.create(
        canonical_url="https://example.com/jobs/1",
        title="Python engineer",
        company="Example",
        location="Tel Aviv",
        description="Python backend and Linux",
    )


@pytest.fixture
def application(user, job):
    return Application.objects.create(
        owner=user,
        job=job,
        recipient="hiring@example.com",
        subject="Application",
        body="My application",
    )


@pytest.fixture
def connection(user):
    return GmailConnection.objects.create(
        owner=user, email="alice@example.com", encrypted_credentials="test-not-a-token"
    )


@pytest.fixture
def authenticated(client, user):
    client.force_login(user)
    return client


@pytest.fixture(scope="session")
def playwright(django_db_setup):
    """Playwright owns a loop on this thread; test HTTP runs on Django's separate thread.

    Only browser tests get this override. Production async-safety checks remain enabled.
    """
    from playwright.sync_api import sync_playwright

    previous = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    try:
        with sync_playwright() as instance:
            yield instance
    finally:
        if previous is None:
            os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
        else:
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = previous


@pytest.fixture
def agent_settings(settings):
    settings.AGENT_BACKEND = "infinity"
    settings.INFINITY_SERVICE_TOKEN = "test-service-token-with-at-least-32-chars"
    settings.INFINITY_TOOL_SECRET = "test-tool-signing-secret-not-real"
    settings.INFINITY_MODEL = "fake"
    settings.AI_DAILY_TOKEN_BUDGET = 1000000


@pytest.fixture
def evaluation_profile(user):
    from jobs.models import Document, ResumeProfile

    document = Document.objects.create(
        owner=user, name="CV", file="unused.pdf", sha256="a" * 64, size=1
    )
    return ResumeProfile.objects.create(
        owner=user,
        document=document,
        name="Confirmed",
        facts=[{"id": "f1", "text": "Python and Linux"}],
    )
