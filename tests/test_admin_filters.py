import base64
import hashlib
from io import StringIO

import pytest
from bs4 import BeautifulSoup
from django.core.management import call_command
from django_otp import DEVICE_ID_SESSION_KEY

from jobs.models import Job
from jobs.services.normalization import normalize_description

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(client, user):
    user.is_staff = user.is_superuser = True
    user.save()
    device = user.totpdevice_set.create(name="synthetic-filter-test", confirmed=True)
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return client


def posting(title, location, company, description):
    return Job.objects.create(
        title=title,
        location=location,
        company=company,
        canonical_url=f"https://example.com/{title}",
        description=description,
        normalized=normalize_description(description),
    )


def results(client, **params):
    response = client.get("/admin/jobs/job/", params)
    assert response.status_code == 200
    return list(response.context["cl"].result_list)


def test_combined_location_company_and_technology_filters(staff_client):
    required = posting("Backend", "Tel Aviv, Israel", "Acme", "Requirements\nPython and Node.js")
    preferred = posting(
        "Platform", "Haifa, Israel", "Acme", "Requirements\nRust\nNice to have\nPython"
    )
    elsewhere = posting("Other", "Remote, US", "Other", "Requirements\nPython")
    assert results(staff_client, location="tel aviv", company="acme", technology="Python") == [
        required
    ]
    assert results(staff_client, location="ISRAEL", technology="Python") == [preferred, required]
    assert results(staff_client, technology="Python", technology_scope="required") == [
        elsewhere,
        required,
    ]
    assert results(staff_client, technology="Node.js") == [required]
    assert results(staff_client, technology="Rust", technology_scope="required") == [preferred]
    assert results(staff_client, technology="unknown") == []
    assert results(staff_client, location="<script>bad()</script>") == []
    assert results(staff_client, location="", company="", q="Haifa") == [preferred]


def test_admin_theme_csp_and_permissions(staff_client, client):
    response = staff_client.get("/admin/jobs/job/")
    assert b"adminlte" in response.content and b"Location contains" in response.content
    soup = BeautifulSoup(response.content, "html.parser")
    policy = response["Content-Security-Policy"]
    script_policy = next(part for part in policy.split(";") if "script-src" in part)
    assert "unsafe-inline" not in script_policy and "unsafe-eval" not in script_policy
    scripts = soup.find_all("script", src=False)
    assert scripts
    for script in scripts:
        digest = base64.b64encode(hashlib.sha256((script.string or "").encode()).digest()).decode()
        assert f"'sha256-{digest}'" in script_policy
    assert not soup.select('link[href*="fonts.googleapis.com"]')
    client.logout()
    assert client.get("/admin/jobs/job/").status_code == 302


def test_refresh_existing_tags_preserves_curated_content(staff_client):
    job = posting("Node", "Remote", "Acme", "Requirements\nNodeJS\nNice to have\nRust")
    job.normalized = {"requirements": ["NodeJS"], "summary": "Curated summary"}
    job.content_overrides = {"title": "Curated", "normalized": dict(job.normalized)}
    job.save()
    output = StringIO()
    call_command("refresh_job_technologies", dry_run=True, stdout=output)
    job.refresh_from_db()
    assert "technologies" not in job.normalized
    call_command("refresh_job_technologies", stdout=output)
    job.refresh_from_db()
    assert job.normalized["technologies"] == ["Rust", "Node.js"]
    assert job.normalized["required_technologies"] == ["Node.js"]
    assert job.normalized["summary"] == "Curated summary"
    assert job.content_overrides["normalized"]["required_technologies"] == ["Node.js"]
    assert job.content_overrides["title"] == "Curated"
    assert results(staff_client, technology="Node.js", technology_scope="required") == [job]
    output = StringIO()
    call_command("refresh_job_technologies", stdout=output)
    assert output.getvalue().startswith("0 jobs")


def test_mfa_still_required(client, user):
    user.is_staff = user.is_superuser = True
    user.save()
    client.force_login(user)
    response = client.get("/admin/jobs/job/")
    assert response.status_code == 302 and response.url.startswith("/account/")


def test_manifest_directory_compatibility_does_not_relax_missing_assets(settings, tmp_path):
    from config.static_storage import AdminStaticFilesStorage

    settings.STATIC_URL = "/static/"
    storage = AdminStaticFilesStorage(location=tmp_path)
    assert storage.url("vendor/bootswatch") == "/static/vendor/bootswatch"
    with pytest.raises(ValueError, match="Missing staticfiles manifest entry"):
        storage.url("not-a-real-asset.js")
