"""GitLab's normal Greenhouse UI. Ephemeral, pinned origins, one final POST only."""

import json
import re
import time
from contextlib import suppress
from urllib.parse import urlsplit

from playwright.sync_api import Error, sync_playwright

from .cato_browser import (
    HOSTS,
    UPLOAD_HOST,
    VerificationGate,
    challenge,
    country_options,
    launch,
    resolver_rules,
)
from .gitlab_greenhouse import Schema, project_questions
from .secret_telaviv import PortalRateLimited, PortalRejected, PortalUnknown

ROUTE = "routes/$url_token_.jobs_.$job_post_id"


def confirmation(url, identity):
    p = urlsplit(url)
    return (
        p.scheme == "https"
        and p.hostname == "job-boards.greenhouse.io"
        and p.port in (None, 443)
        and not p.username
        and not p.password
        and p.path == f"/gitlab/jobs/{identity}/confirmation"
    )


def check_ui(page, identity, schema):
    data = page.evaluate("""() => {
        const key = 'routes/$url_token_.jobs_.$job_post_id';
        const d = window.__remixContext?.state?.loaderData?.[key];
        return d ? {submit:d.submitPath, confirmation:d.confirmationPath,
                    questions:d.jobPost.questions, education:d.jobPost.education_config,
                    redirect:d.jobPost.redirect_to} : null;
    }""")
    try:
        if (
            not data
            or data["submit"] != f"https://boards.greenhouse.io/gitlab/jobs/{identity}"
            or not confirmation("https://job-boards.greenhouse.io" + data["confirmation"], identity)
            or project_questions(data["questions"]) != schema.payload["questions"]
            or data.get("redirect")
            or any(v != "hidden" for k, v in (data.get("education") or {}).items() if k != "id")
        ):
            raise ValueError
    except TypeError, KeyError, ValueError:
        raise PortalRejected("gitlab_form_changed") from None


def fill(page, answers, schema):
    # Prove React hydration using a harmless dropdown before writing applicant data.
    try:
        options = country_options(page)
    except PortalRejected as exc:
        raise PortalRejected("gitlab_form_not_interactive") from exc
    if answers.get("phone"):
        country = page.evaluate(
            "r => new Intl.DisplayNames(['en'], {type:'region'}).of(r)", answers["phone_country"]
        )
        match = options.filter(has_text=re.compile(r"^" + re.escape(country) + r"(?:\s|\+|$)"))
        if match.count() != 1:
            raise PortalRejected("gitlab_phone_country_needs_review")
        match.click()
    else:
        page.get_by_role("group", name="Phone", exact=True).get_by_role(
            "button", name="Toggle flyout"
        ).click()
    for q in schema.entries:
        f = q["fields"][0]
        value = answers.get(f["name"], "")
        if not value:
            continue
        control = page.locator("#" + f["name"])
        if f["type"] == "multi_value_single_select":
            label = next(v["label"] for v in f["values"] if v["value"] == value)
            control.click()
            control.fill(label)
            option = page.get_by_role("option", name=label, exact=True)
            option.wait_for(state="visible")
            if option.count() != 1:
                raise PortalRejected("gitlab_choice_needs_review")
            option.click()
        else:
            control.fill(value)


def run(
    context,
    identity,
    answers,
    attachment,
    schema: Schema,
    *,
    outcome_timeout=45000,
    on_human_verification=None,
    assisted_timeout=600,
):
    target = f"https://job-boards.greenhouse.io/gitlab/jobs/{identity}"
    submit = f"https://boards.greenhouse.io/gitlab/jobs/{identity}"
    attempted, posting, status, retry_after, response_payload = False, False, None, "", None
    payload_error = False
    # Greenhouse's explicit rejection protocol is shared with Cato. The gate freezes
    # the entire initial application (including its uploaded CV URL), permits only
    # three code attempts, and closes permanently after any uncertain request.
    gate = VerificationGate(email=answers["email"])
    session_deadline = time.monotonic() + assisted_timeout
    stage = "navigation"

    def guard(route):
        nonlocal attempted, payload_error, status, response_payload
        r = route.request
        p = urlsplit(r.url)
        allowed = (
            p.scheme == "https"
            and p.hostname in HOSTS
            and p.port in (None, 443)
            and not p.username
            and not p.password
        )
        if r.method not in {"GET", "HEAD", "OPTIONS"}:
            allowed = allowed and (
                (
                    r.method == "POST"
                    and r.url == submit
                    and posting
                    and (not attempted or (on_human_verification is not None and gate.pending))
                )
                or (r.method == "POST" and p.hostname == UPLOAD_HOST)
                or p.hostname in {"www.recaptcha.net", "www.google.com"}
            )
        if r.url == submit and r.method == "POST" and allowed:
            try:
                payload = json.loads(r.post_data or "{}")
                application = payload["job_application"]
                resume = urlsplit(application["resume_url"])
                if (
                    resume.scheme != "https"
                    or resume.hostname != UPLOAD_HOST
                    or resume.port not in (None, 443)
                    or resume.username
                    or resume.password
                    or application.get("resume_url_filename") != "resume.pdf"
                    or any(
                        application.get(k, "").strip() != answers[k].strip()
                        for k in ("first_name", "last_name", "email")
                    )
                ):
                    raise ValueError
            except ValueError, KeyError, TypeError, AttributeError:
                payload_error = True
                allowed = False
            else:
                allowed = gate.permit(payload)
                if allowed:
                    attempted = True
                    status = None
                    response_payload = None
        if time.monotonic() >= session_deadline:
            allowed = False
        if allowed:
            route.fallback()
        else:
            route.abort()

    def received(response):
        nonlocal status, retry_after, response_payload
        if response.url == submit and response.request.method == "POST":
            status = response.status
            retry_after = response.headers.get("retry-after", "")
            if status in {400, 403, 422, 428}:
                with suppress(ValueError, Error):
                    response_payload = response.json()
            gate.observe(status, response_payload)

    def verification_error():
        return gate.error.replace("cato_", "gitlab_", 1)

    def wait_for_human():
        if on_human_verification is None:
            raise PortalRejected("gitlab_human_verification_required")
        on_human_verification(page)
        while challenge(page):
            if time.monotonic() >= session_deadline:
                raise PortalRejected("gitlab_assisted_session_timeout")
            page.wait_for_timeout(100)

    context.route("**/*", guard)
    page = context.new_page()
    page.set_default_timeout(15000)
    page.on("response", received)
    try:
        nav = page.goto(target, wait_until="domcontentloaded", timeout=30000)
        if nav and nav.status == 429:
            raise PortalRateLimited(nav.headers.get("retry-after", ""))
        if not nav or nav.status != 200:
            raise PortalRejected("gitlab_vacancy_unavailable")
        page.locator("#first_name").wait_for(state="visible")
        check_ui(page, identity, schema)
        if challenge(page):
            wait_for_human()
        stage = "answers"
        fill(page, answers, schema)
        stage = "upload"
        with page.expect_response(
            lambda r: urlsplit(r.url).hostname == UPLOAD_HOST and r.request.method == "POST",
            timeout=30000,
        ) as uploaded:
            page.locator("#resume").set_input_files(
                {"name": "resume.pdf", "mimeType": "application/pdf", "buffer": attachment}
            )
        if uploaded.value.status == 429:
            raise PortalRateLimited(uploaded.value.headers.get("retry-after", ""))
        if uploaded.value.status != 201:
            raise PortalRejected("gitlab_cv_upload_failed")
        # Wait for the UI's uploaded-file state, not merely the S3 response headers.
        page.get_by_text("resume.pdf", exact=True).wait_for(state="visible", timeout=30000)
        if challenge(page):
            wait_for_human()
        stage = "submit"
        posting = True
        page.get_by_role("button", name="Submit application", exact=True).click(timeout=30000)
        deadline = time.monotonic() + outcome_timeout / 1000
        notified_posts = 0
        while not confirmation(page.url, identity):
            if gate.error:
                raise PortalRejected(verification_error())
            if time.monotonic() >= session_deadline:
                if gate.uncertain:
                    raise PortalUnknown("gitlab_submission_outcome_unknown")
                raise PortalRejected("gitlab_assisted_session_timeout")
            if payload_error:
                raise PortalRejected("gitlab_reviewed_content_not_bound")
            if not gate.uncertain and not gate.pending and challenge(page):
                wait_for_human()
                deadline = time.monotonic() + outcome_timeout / 1000
            if status == 429:
                raise PortalRateLimited(retry_after)
            if status in {400, 401, 403, 404, 413, 422, 428}:
                if isinstance(response_payload, dict) and response_payload.get("code") in {
                    "captcha-failed",
                    "invalid-security-code",
                    "expired-security-code",
                }:
                    if gate.pending and on_human_verification is not None:
                        if notified_posts != gate.posts:
                            notified_posts = gate.posts
                            on_human_verification(page)
                        page.wait_for_timeout(100)
                        continue
                    raise PortalRejected("gitlab_human_verification_required")
                raise PortalRejected("gitlab_application_rejected")
            if on_human_verification is not None and gate.posts > notified_posts and gate.posts > 1:
                deadline = time.monotonic() + outcome_timeout / 1000
                notified_posts = gate.posts
            if time.monotonic() >= deadline:
                if gate.uncertain:
                    raise PortalUnknown("gitlab_submission_outcome_unknown")
                raise PortalRejected("gitlab_form_needs_review")
            page.wait_for_timeout(100)
        if status is None or not 200 <= status < 300:
            raise PortalUnknown("gitlab_confirmation_unverified")
        return f"gitlab_greenhouse:{identity}"
    except PortalRejected, PortalUnknown, PortalRateLimited:
        raise
    except Exception as exc:
        if attempted:
            raise PortalUnknown("gitlab_submission_outcome_unknown") from exc
        raise PortalRejected(f"gitlab_{stage}_unavailable") from exc
    finally:
        with suppress(Error):
            context.close()


def deliver(identity, answers, attachment, schema, *, on_human_verification=None, assisted=False):
    started = False
    try:
        rules = resolver_rules()
        with sync_playwright() as p:
            browser = launch(p, rules, headless=False) if assisted else launch(p, rules)
            try:
                context = browser.new_context(service_workers="block", accept_downloads=False)
                started = True
                return run(
                    context,
                    identity,
                    answers,
                    attachment,
                    schema,
                    on_human_verification=on_human_verification,
                )
            finally:
                with suppress(Error):
                    browser.close()
    except (Error, OSError) as exc:
        if started:
            raise PortalUnknown("gitlab_browser_outcome_unknown") from exc
        raise PortalRejected("gitlab_browser_unavailable") from exc


def probe(identity, schema):
    """No applicant values, CV upload, or final POST in this live readiness check."""
    with sync_playwright() as p:
        browser = launch(p, resolver_rules())
        try:
            context = browser.new_context(service_workers="block", accept_downloads=False)

            def guard(route):
                u = urlsplit(route.request.url)
                if (
                    u.scheme == "https"
                    and u.hostname in HOSTS
                    and route.request.method in {"GET", "HEAD", "OPTIONS"}
                ):
                    route.continue_()
                else:
                    route.abort()

            context.route("**/*", guard)
            page = context.new_page()
            nav = page.goto(
                f"https://job-boards.greenhouse.io/gitlab/jobs/{identity}",
                wait_until="domcontentloaded",
            )
            if not nav or nav.status != 200:
                raise PortalRejected("gitlab_vacancy_unavailable")
            page.locator("#first_name").wait_for(state="visible")
            check_ui(page, identity, schema)
            interactive = bool(country_options(page).count())
            return {
                "schema_matches": True,
                "form_interactive": interactive,
                "controls_present": all(
                    page.locator("#" + q["fields"][0]["name"]).count() == 1 for q in schema.entries
                ),
                "human_challenge": challenge(page),
            }
        finally:
            with suppress(Error):
                browser.close()
