import pytest
from django.contrib.auth.models import User
from playwright.sync_api import expect

from jobs.models import Job

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_search_save_and_prepare(page, live_server, settings):
    # Django's test StaticFilesHandler uses source finders, not collected hashed assets.
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    User.objects.create_user("browser-user", password="browser-only-password-871")
    Job.objects.create(
        canonical_url="https://example.com/browser-job",
        title="Backend Engineer",
        company="Example",
        location="Tel Aviv",
        description="Python and Linux",
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(live_server.url)
    expect(page.locator("body")).to_have_css("background-color", "rgb(20, 23, 27)")
    assert page.evaluate("typeof htmx") == "object"
    page.get_by_label("Username").fill("browser-user")
    page.get_by_label("Password").fill("browser-only-password-871")
    page.get_by_role("button", name="Next").click()
    expect(page.get_by_role("heading", name="Find your next job")).to_be_visible()
    expect(
        page.get_by_role("navigation", name="Main").get_by_role("link", name="Jobs")
    ).to_have_attribute("aria-current", "page")
    expect(page.get_by_role("heading", name="Explore opportunities")).to_be_visible()
    page.screenshot(path="/tmp/jobstream-jobs-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name="Search", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="/tmp/jobstream-jobs-mobile.png", full_page=True)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.get_by_label("Keywords").fill("Python")
    page.get_by_role("button", name="Search", exact=True).click()
    page.get_by_role("link", name="Backend Engineer").click()
    page.get_by_role("button", name="Save job", exact=True).click()
    expect(page.get_by_role("button", name="Unsave")).to_be_visible()
    page.get_by_role("link", name="Prepare application").click()
    page.get_by_label("Recipient").fill("hr@example.com")
    page.get_by_label("Body").fill("Please review my application.")
    page.get_by_role("button", name="Save draft").click()
    expect(page.get_by_text("Prepared", exact=True)).to_be_visible()
    page.get_by_role("button", name="Approve and send this application").click()
    expect(page.get_by_text("Connect Gmail before approving.", exact=False)).to_be_visible()
    page.screenshot(path="/tmp/jobstream-browser-desktop.png", full_page=True)
    assert not errors
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_mfa_qr_has_white_background(page, live_server, settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    User.objects.create_user("qr-test-user", password="browser-only-password-629")
    page.goto(live_server.url + "/account/login/")
    page.get_by_label("Username").fill("qr-test-user")
    page.get_by_label("Password").fill("browser-only-password-629")
    page.get_by_role("button", name="Next").click()
    page.goto(live_server.url + "/account/two_factor/setup/")
    page.get_by_role("button", name="Next").click()
    qr = page.get_by_alt_text("QR Code", exact=True)
    expect(qr).to_be_visible()
    expect(qr).to_have_css("background-color", "rgb(255, 255, 255)")
    expect(qr).to_have_css("padding", "16px")
    assert qr.evaluate("image => image.complete && image.naturalWidth > 0")


def test_mfa_enrollment_accepts_standard_totp(page, live_server, settings):
    """Exercise the displayed enrollment URI using an independent RFC 6238 calculation."""
    import base64
    import hashlib
    import hmac
    import struct
    import time
    from urllib.parse import parse_qs, urlsplit

    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    User.objects.create_user("totp-test-user", password="browser-only-password-781", is_staff=True)
    page.goto(live_server.url + "/account/login/")
    page.get_by_label("Username").fill("totp-test-user")
    page.get_by_label("Password").fill("browser-only-password-781")
    page.get_by_role("button", name="Next").click()
    page.goto(live_server.url + "/account/two_factor/setup/")
    page.get_by_role("button", name="Next").click()
    uri = page.locator('a[href^="otpauth:"]').get_attribute("href")
    parameters = parse_qs(urlsplit(uri).query)
    key = base64.b32decode(parameters["secret"][0])
    digest = hmac.new(key, struct.pack(">Q", int(time.time()) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000
    page.get_by_label("Token").fill(f"{code:06d}")
    page.get_by_role("button", name="Next").click()
    expect(page).to_have_url(live_server.url + "/account/two_factor/setup/complete/")
    assert (
        User.objects.get(username="totp-test-user").totpdevice_set.filter(confirmed=True).exists()
    )


def test_company_picker_and_profile(page, live_server, settings):
    from jobs.models import Company, Source
    from jobs.services.companies import resolve_company

    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    User.objects.create_user("company-browser", password="browser-only-password-931")
    source = Source.objects.create(name="Company feed", adapter="rss", url="https://example.com")
    company = resolve_company("Acme", source)
    other = Company.objects.create(name="Acme", identity_key="other-company")
    for profile, title in [(company, "Platform Engineer"), (other, "Product Designer")]:
        Job.objects.create(
            title=title,
            company="Acme",
            company_profile=profile,
            canonical_url=f"https://example.com/job/{profile.pk}",
        )
    page.goto(live_server.url)
    page.get_by_label("Username").fill("company-browser")
    page.get_by_label("Password").fill("browser-only-password-931")
    page.get_by_role("button", name="Next").click()
    picker = page.get_by_role("combobox", name="Company", exact=True)
    picker.fill("Acme")
    expect(page.get_by_role("option", name="Acme · Company feed", exact=True)).to_be_visible()
    picker.press("ArrowDown")  # All companies
    picker.press("ArrowDown")  # Exact company from this feed
    picker.press("Enter")
    expect(picker).to_have_value("Acme · Company feed")
    page.get_by_role("button", name="Search", exact=True).click()
    expect(page.get_by_role("link", name="Platform Engineer")).to_be_visible()
    expect(page.get_by_role("link", name="Product Designer")).to_have_count(0)
    page.get_by_role("link", name="Acme", exact=True).click()
    expect(page).to_have_url(f"{live_server.url}/companies/{company.pk}/")
    expect(page.get_by_role("heading", name="Acme", exact=True)).to_be_visible()
    page.get_by_role("link", name="All opportunities").click()
    picker = page.get_by_role("combobox", name="Company", exact=True)
    picker.fill("Does not exist")
    page.get_by_role("button", name="Search", exact=True).click()
    assert not picker.evaluate("element => element.validity.valid")
    picker.press("Escape")
    expect(picker).to_have_value("")
    page.get_by_role("button", name="Search", exact=True).click()
    expect(page.get_by_role("link", name="Product Designer")).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    picker.fill("Acme")
    expect(page.get_by_role("option", name="Acme · Company feed", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="/tmp/jobstream-company-picker-mobile.png", full_page=True)


def test_batch_select_profile_and_review(page, live_server, user, job, settings, tmp_path):
    from django.core.files.base import ContentFile

    from jobs.models import ApplicationBatch
    from jobs.services.batches import prepare
    from jobs.services.documents import upload_document
    from jobs.services.resumes import pdf_bytes

    settings.MEDIA_ROOT = tmp_path
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    data = pdf_bytes("Alice", "alice@example.com", [{"section": "Skills", "text": "Python, Linux"}])
    document = upload_document(user, ContentFile(data, name="base.pdf"))
    job.apply_email = "jobs@example.com"
    job.save()
    page.goto(live_server.url)
    page.get_by_label("Username").fill("alice")
    page.get_by_label("Password").fill("a-long-test-password-123")
    page.get_by_role("button", name="Next").click()
    page.goto(f"{live_server.url}/preferences/")
    exclusions = page.get_by_role(
        "group", name="Do not apply when these technologies are mandatory"
    )
    exclusions.get_by_label("Java", exact=True).check()
    exclusions.get_by_label("Node.js", exact=True).check()
    page.get_by_role("button", name="Save preferences").click()
    expect(exclusions.get_by_label("Node.js", exact=True)).to_be_checked()
    page.goto(f"{live_server.url}/documents/{document.pk}/profile/")
    page.get_by_label("Full name").fill("Alice")
    page.locator("#id_skills").fill("Python, Linux")
    page.locator("#id_other").fill("")
    page.get_by_label("I confirm these facts", exact=False).check()
    page.get_by_role("button", name="Confirm and save profile").click()
    profile = document.profiles.get()
    page.goto(live_server.url)
    page.get_by_label("Select Python engineer", exact=True).check()
    page.get_by_role("button", name="Apply to selected jobs").click()
    page.locator("select[name=document]").select_option(str(document.pk))
    page.locator("select[name=profile]").select_option(str(profile.pk))
    page.get_by_role("button", name="Prepare selected applications").click()
    expect(page.get_by_role("heading", name="Application batch", exact=False)).to_be_visible()
    batch = ApplicationBatch.objects.get(owner=user)
    item = batch.items.get()
    prepare(item.pk)
    expect(page.get_by_text("CV tailored from your reviewed profile.", exact=True)).to_be_visible(
        timeout=10000
    )
    selection = page.get_by_role("checkbox", name="Approve Python engineer")
    selection.check()
    # Force another real progress request and verify that the user's checked item survives.
    with page.expect_response(lambda response: response.url.endswith(f"/batches/{batch.pk}/")):
        page.evaluate(
            "htmx.ajax('GET', location.href, {target: '#batch-items', swap: 'outerHTML'})"
        )
    expect(selection).to_be_checked()
    page.get_by_role("link", name="Review application").click()
    expect(page.get_by_text("Python, Linux", exact=False)).to_be_visible()
    page.screenshot(path="/tmp/jobstream-batch-application.png", full_page=True)


def test_secret_telaviv_review_and_approve_without_gmail(
    page, live_server, user, job, settings, tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from django.core.files.base import ContentFile

    from jobs.models import Application
    from jobs.services import secret_telaviv as sta
    from jobs.services import submissions
    from jobs.services.documents import upload_document
    from jobs.services.resumes import pdf_bytes
    from tests.test_secret_telaviv import HTML, URL

    settings.MEDIA_ROOT = tmp_path
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    job.apply_url = URL
    job.save()
    document = upload_document(
        user,
        ContentFile(
            pdf_bytes(
                "Test Applicant",
                "test@example.invalid",
                [{"section": "Skills", "text": "Synthetic test data, not a real candidate."}],
            ),
            name="test.pdf",
        ),
    )
    session = Mock()
    session.request.side_effect = [
        sta.Response(200, {}, HTML),
        sta.Response(200, {}, HTML),
        sta.Response(302, {"location": URL + "applied/123/"}, b""),
        sta.Response(
            200,
            {},
            b'<div class="wpjb-flash-info">Your application has been sent.</div>'
            b'<span class="wpjb-error"></span>',
        ),
    ]
    monkeypatch.setattr(sta, "SecretSession", lambda: session)
    page.goto(live_server.url)
    page.get_by_label("Username").fill("alice")
    page.get_by_label("Password").fill("a-long-test-password-123")
    page.get_by_role("button", name="Next").click()
    page.goto(f"{live_server.url}/jobs/{job.pk}/apply/")
    page.locator("select[name=document]").select_option(str(document.pk))
    page.get_by_label("First name", exact=False).fill("Test")
    page.get_by_label("Family name", exact=False).fill("Applicant")
    page.get_by_label("Applicant email", exact=False).fill("test@example.invalid")
    page.get_by_label("Telephone", exact=False).fill("0000000000")
    page.get_by_label("I accept the Secret Tel Aviv", exact=False).check()
    expect(page.get_by_label("Forward my CV to headhunters")).not_to_be_checked()
    page.get_by_role("button", name="Save draft").click()
    expect(
        page.get_by_role("heading", name="Secret Tel Aviv application", exact=True)
    ).to_be_visible()
    page.get_by_role("button", name="Approve and send this application").click()
    app = Application.objects.get(owner=user, job=job)
    assert app.status == "approved"
    submissions.submit(app.pk)
    page.reload()
    expect(page.get_by_text("Confirmed sent", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Employer receipt page")).to_be_visible()
    page.screenshot(path="/tmp/jobstream-secret-telaviv-confirmed.png", full_page=True)


def test_jazzmin_admin_filters(page, live_server, settings, client):
    from django_otp import DEVICE_ID_SESSION_KEY

    from jobs.services.normalization import normalize_description

    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    user = User.objects.create_superuser("admin-theme-test", password="browser-only-password-761")
    device = user.totpdevice_set.create(name="synthetic-admin-browser", confirmed=True)
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
                "url": live_server.url,
            }
        ]
    )
    for title, location, desc in [
        ("Python Engineer", "Tel Aviv, Israel", "Requirements\nPython\nNice to have\nRust"),
        ("Rust Engineer", "Haifa, Israel", "Requirements\nRust"),
    ]:
        Job.objects.create(
            title=title,
            location=location,
            company="Acme",
            canonical_url=f"https://example.com/{title.replace(' ', '-')}",
            description=desc,
            normalized=normalize_description(desc),
        )
    errors = []
    violations = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            violations.append(message.text)
            if message.type == "error" and "Content Security Policy" in message.text
            else None
        ),
    )
    page.goto(live_server.url + "/admin/jobs/job/")
    expect(page.locator("#jazzy-navbar")).to_be_visible()
    page.get_by_label("Location contains", exact=True).fill("tel aviv")
    page.get_by_label("Company contains", exact=True).fill("Acme")
    # Jazzmin enhances these with Select2; exercise the visible controls.
    tech = page.locator('select[data-name="technology"] + span')
    tech.click()
    page.get_by_role("option", name="Python", exact=True).click()
    page.get_by_role("button", name="Search", exact=True).click()
    expect(page.get_by_role("link", name="Python Engineer", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Rust Engineer", exact=True)).to_have_count(0)
    expect(page.get_by_label("Location contains", exact=True)).to_have_value("tel aviv")
    assert "technology=Python" in page.url
    page.screenshot(path="/tmp/jobstream-admin-refresh.png", full_page=True)
    page.get_by_role("link", name="Python Engineer", exact=True).click()
    expect(page.locator("#id_title")).to_have_value("Python Engineer")
    assert not errors and not violations


def test_my_preferences_and_common_answers(page, live_server, settings, client, user):
    from cryptography.fernet import Fernet

    from jobs.models import ApplicationPreferences, SavedApplicationAnswers

    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    client.force_login(user)
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
                "url": live_server.url,
            }
        ]
    )
    page.goto(live_server.url + "/preferences/")
    expect(page.get_by_role("link", name="My preferences", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    expect(page.locator('nav[aria-label="Main"] [aria-current="page"]')).to_have_count(1)
    page.get_by_label("Preferred locations", exact=False).fill("Israel\nTel Aviv")
    page.get_by_label("Preferred job titles", exact=False).fill("Backend\nSecurity")
    page.get_by_role("group", name="Technologies I want to work with", exact=True).get_by_label(
        "Python", exact=True
    ).check()
    page.get_by_role(
        "group", name="Do not apply when these technologies are mandatory", exact=True
    ).get_by_label("Java", exact=True).check()
    page.get_by_role("button", name="Save preferences", exact=True).click()
    pref = ApplicationPreferences.objects.get(owner=user)
    assert pref.wanted_technologies == ["Python"] and pref.preferred_locations == [
        "Israel",
        "Tel Aviv",
    ]
    page.get_by_label("First name", exact=False).fill("Alice")
    page.get_by_label("Family name", exact=False).fill("Example")
    page.get_by_label("Application email", exact=False).fill("alice@example.test")
    page.get_by_label("Gender identity (optional)", exact=False).select_option(
        "prefer_not_to_answer"
    )
    page.get_by_label("Allow these saved answers", exact=False).check()
    page.get_by_role("button", name="Save common answers", exact=True).click()
    record = SavedApplicationAnswers.objects.get(owner=user)
    assert "alice@example.test" not in record.encrypted_answers and record.reuse_enabled
    expect(page.get_by_label("Application email", exact=False)).to_have_value("alice@example.test")
    page.screenshot(path="/tmp/jobstream-my-preferences-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="/tmp/jobstream-my-preferences-mobile.png", full_page=True)
    page.get_by_role("button", name="Delete saved answers", exact=True).click()
    assert not SavedApplicationAnswers.objects.filter(owner=user).exists()
    expect(page.get_by_label("Application email", exact=False)).to_have_value("")
