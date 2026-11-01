"""Isolated Chromium UI transport. No API credentials, persisted session, or CAPTCHA bypass."""

import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error, TimeoutError, sync_playwright

from .network import public_addresses
from .secret_telaviv import PortalRateLimited, PortalRejected, PortalUnknown

# Resolve once before starting Chromium and pin every allowed origin to a public IP.
# Default-deny routing and resolver rules also prevent private-network subresources.
HOSTS = frozenset(
    {
        "job-boards.greenhouse.io",
        "job-boards.cdn.greenhouse.io",
        "boards.greenhouse.io",
        "www.recaptcha.net",
        "www.gstatic.com",
        "www.google.com",
        "grnhse-prod-jben-us-east-1.s3.amazonaws.com",
        "api-geocode-earth-proxy.greenhouse.io",
    }
)
UPLOAD_HOST = "grnhse-prod-jben-us-east-1.s3.amazonaws.com"


@dataclass
class VerificationGate:
    """A follow-up POST is safe only after a recognized, explicit verification rejection."""

    email: str
    posts: int = 0
    uncertain: bool = False
    pending: bool = False
    original_application: dict | None = None
    error: str = ""
    changed_fields: list[str] = field(default_factory=list)

    @staticmethod
    def reviewed_content(application):
        # Provider-owned attribution/time-zone metadata is not applicant content.
        content = {
            k: v
            for k, v in application.items()
            if k not in {"mapped_url_token", "appcast_click_id", "time_zone"}
        }
        for key in ("first_name", "last_name", "email"):
            if isinstance(content.get(key), str):
                content[key] = content[key].strip()
        if isinstance(content.get("email"), str):
            content["email"] = content["email"].casefold()
        if isinstance(content.get("phone"), str):
            content["phone"] = re.sub(r"[\s()-]", "", content["phone"])
        return content

    def permit(self, payload) -> bool:
        if self.error or self.uncertain or not isinstance(payload, dict):
            return False
        application = payload.get("job_application")
        if not isinstance(application, dict):
            self.error = "cato_submission_payload_changed"
            return False
        content = self.reviewed_content(application)
        if self.posts:
            original = self.original_application
            if original is None:
                self.error = "cato_submission_payload_changed"
                return False
            if not self.pending:
                return False
            if self.posts >= 4:
                self.error = "cato_verification_attempts_exhausted"
                return False
            code = payload.get("security_code")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9-]{8}", code):
                self.error = "cato_verification_code_required"
                return False
            if content != original:
                self.changed_fields = sorted(
                    k for k in content.keys() | original.keys() if content.get(k) != original.get(k)
                )
                self.error = "cato_reviewed_content_changed"
                return False
        else:
            self.original_application = json.loads(json.dumps(content))
        self.posts += 1
        self.uncertain = True
        self.pending = False
        return True

    def observe(self, status, payload):
        if status not in {400, 403, 422, 428} or not isinstance(payload, dict):
            return
        code = payload.get("code")
        if code == "captcha-failed":
            self.uncertain = False
            recipient = payload.get("security_code_recipient")
            if not isinstance(recipient, str):
                self.error = "cato_human_verification_required"
            elif recipient.strip().casefold() != self.email.strip().casefold():
                self.error = "cato_verification_recipient_changed"
            else:
                self.pending = True
        elif code == "invalid-security-code" and self.posts > 1:
            self.uncertain = False
            if self.posts < 4:
                self.pending = True
            else:
                self.error = "cato_verification_attempts_exhausted"
        elif code in {"expired-security-code", "security-code-error"}:
            self.uncertain = False
            self.error = "cato_verification_code_expired_or_locked"


def resolver_rules() -> str:
    return ", ".join(
        [f"MAP {host} {public_addresses(host)[0]}" for host in sorted(HOSTS)] + ["MAP * ~NOTFOUND"]
    )


def confirmation(url: str, identity: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.netloc == "job-boards.greenhouse.io"
        and parts.path == "/embed/job_app/confirmation"
        and parse_qs(parts.query).get("for") == ["catonetworks"]
        and parse_qs(parts.query).get("token") == [identity]
    )


def challenge(page) -> bool:
    # Invisible enterprise protection is normal; an interactive challenge needs a person.
    return (
        page.locator(
            'iframe[src*="bframe"], iframe[title*="challenge" i], input[name="security_code"]'
        )
        .filter(visible=True)
        .count()
        > 0
    )


def country_options(page):
    """SSR controls are visible before React attaches handlers. Prove interaction first.

    Retrying this harmless dropdown operation is safe; never retry upload/Submit clicks.
    """
    button = page.get_by_role("group", name="Phone", exact=True).get_by_role(
        "button", name="Toggle flyout"
    )
    options = page.get_by_role("option")
    for _ in range(5):
        if options.count():
            return options
        button.click()
        try:
            options.first.wait_for(state="visible", timeout=1000)
            return options
        except TimeoutError:
            continue
    raise PortalRejected("cato_form_not_interactive")


def fill_form(page, answers, linkedin_field):
    options = country_options(page)
    country_name = page.evaluate(
        "region => new Intl.DisplayNames(['en'], {type:'region'}).of(region)",
        answers["phone_country"].upper(),
    )
    options = options.filter(has_text=re.compile(r"^" + re.escape(country_name) + r"(?:\s|\+|$)"))
    options.first.wait_for(state="visible")
    if options.count() != 1:
        raise PortalRejected("cato_phone_country_needs_review")
    options.click()
    # No personal data is entered until hydration has demonstrably completed.
    for name in ("first_name", "last_name", "email"):
        page.locator(f"#{name}").fill(answers[name])
    page.locator("#phone").fill(answers["phone"])
    location = page.locator("#candidate-location")
    # React-select filters against the full display label (including region).
    # A comma triggers lookup for short names without filtering out the region.
    city = answers["city"].strip()
    location.fill("")
    location.press_sequentially(city + "," if len(city) < 4 else city, delay=50)
    page.get_by_role("option").first.wait_for(state="visible")
    # Match city and country explicitly. Never take the first autocomplete suggestion.
    labels = page.get_by_role("option").all_text_contents()
    matches = [
        label
        for label in labels
        if (
            label.split(",")[0].strip().casefold() == answers["city"].strip().casefold()
            and label.split(",")[-1].strip().casefold() == answers["country"].strip().casefold()
        )
    ]
    if len(matches) != 1:
        raise PortalRejected("cato_city_needs_review")
    page.get_by_role("option", name=matches[0], exact=True).click()
    if linkedin_field and answers.get("linkedin"):
        page.locator(f"#{linkedin_field}").fill(answers["linkedin"])


def deliver(identity, answers, attachment, linkedin_field=""):
    transport_started = False
    try:
        rules = resolver_rules()
        with sync_playwright() as runtime:
            browser = launch(runtime, rules)
            try:
                context = browser.new_context(service_workers="block", accept_downloads=False)
                transport_started = True
                return run(context, identity, answers, attachment, linkedin_field)
            finally:
                # Cleanup failures must never turn a completed/uncertain send into safe retry.
                with suppress(Error):
                    browser.close()
    except (Error, OSError) as exc:
        if transport_started:
            raise PortalUnknown("cato_browser_outcome_unknown") from exc
        raise PortalRejected("cato_browser_unavailable") from exc


def launch(runtime, rules):
    return runtime.chromium.launch(
        headless=True,
        chromium_sandbox=True,
        args=[f"--host-resolver-rules={rules}", "--disable-quic", "--no-proxy-server"],
    )


def probe(identity: str) -> dict:
    """Read-only readiness check: no contact details, uploads, or application POSTs."""
    rules = resolver_rules()
    with sync_playwright() as runtime:
        browser = launch(runtime, rules)
        try:
            context = browser.new_context(service_workers="block", accept_downloads=False)

            def guard(route):
                parts = urlsplit(route.request.url)
                if (
                    parts.scheme == "https"
                    and parts.hostname in HOSTS
                    and route.request.method in {"GET", "HEAD", "OPTIONS"}
                ):
                    route.continue_()
                else:
                    route.abort()

            context.route("**/*", guard)
            page = context.new_page()
            page.goto(
                f"https://job-boards.greenhouse.io/embed/job_app?for=catonetworks&token={identity}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            page.locator("#first_name").wait_for(state="visible", timeout=15000)
            interactive = bool(country_options(page).count())
            fields = [
                "first_name",
                "last_name",
                "email",
                "phone",
                "country",
                "candidate-location",
                "resume",
            ]
            return {
                "controls_present": all(page.locator(f"#{key}").count() == 1 for key in fields),
                "human_challenge": challenge(page),
                "form_interactive": interactive,
                "submit_button": page.get_by_role(
                    "button", name="Submit application", exact=True
                ).count()
                == 1,
            }
        finally:
            browser.close()


def run(
    context,
    identity,
    answers,
    attachment,
    linkedin_field="",
    *,
    outcome_timeout=45000,
    on_human_verification=None,
):
    """One context per attempt. Exposed separately for fully intercepted browser fixtures."""
    submit_url = f"https://boards.greenhouse.io/embed/catonetworks/jobs/{identity}"
    target = f"https://job-boards.greenhouse.io/embed/job_app?for=catonetworks&token={identity}"
    gate = VerificationGate(answers["email"])
    posting = False
    response_status = None
    retry_after = ""
    stage = "navigation"

    def guard(route):
        request = route.request
        parts = urlsplit(request.url)
        allowed = (
            parts.scheme == "https"
            and parts.hostname in HOSTS
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
        )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            allowed = allowed and (
                (request.url == submit_url and posting and not gate.uncertain)
                or parts.hostname == UPLOAD_HOST
                or parts.hostname in {"www.recaptcha.net", "www.google.com"}
            )
        if request.method == "POST" and request.url == submit_url and allowed:
            try:
                payload = json.loads(request.post_data or "{}")
            except ValueError:
                payload = None
            allowed = gate.permit(payload)
        if not allowed:
            route.abort()
        else:
            route.fallback()

    def response_received(response):
        nonlocal response_status, retry_after
        if response.url == submit_url and response.request.method == "POST":
            response_status = response.status
            retry_after = response.headers.get("retry-after", "")
            if response.status in {400, 403, 422, 428}:
                try:
                    gate.observe(response.status, response.json())
                except ValueError, Error:
                    pass

    context.route("**/*", guard)
    page = context.new_page()
    page.set_default_timeout(15000)
    page.on("response", response_received)
    try:
        navigation = page.goto(target, wait_until="domcontentloaded", timeout=30000)
        if navigation and navigation.status == 429:
            raise PortalRateLimited(navigation.headers.get("retry-after", ""))
        if not navigation or navigation.status != 200:
            status = navigation.status if navigation is not None else "missing_response"
            raise PortalRejected(f"cato_form_http_{status}")
        page.locator("#first_name").wait_for(state="visible")
        if challenge(page):
            raise PortalRejected("cato_human_verification_required")
        # Check the UI's destination independently from the public question schema.
        destinations = page.evaluate("""() => {
            const data = window.__remixContext?.state?.loaderData?.['routes/embed.job_app'];
            return data ? {submit:data.submitPath, confirmation:data.confirmationPath} : null;
        }""")
        if (
            not destinations
            or destinations["submit"] != submit_url
            or not confirmation(
                "https://job-boards.greenhouse.io" + destinations["confirmation"], identity
            )
        ):
            raise PortalRejected("cato_destination_changed")
        stage = "form"
        fill_form(page, answers, linkedin_field)
        if challenge(page):
            raise PortalRejected("cato_human_verification_required")
        stage = "upload"
        with page.expect_response(
            lambda response: (
                urlsplit(response.url).hostname == UPLOAD_HOST and response.request.method == "POST"
            ),
            timeout=30000,
        ) as uploaded:
            page.locator("#resume").set_input_files(
                {"name": "resume.pdf", "mimeType": "application/pdf", "buffer": attachment}
            )
        if uploaded.value.status == 429:
            raise PortalRateLimited(uploaded.value.headers.get("retry-after", ""))
        if uploaded.value.status != 201:
            raise PortalRejected("cato_cv_upload_failed")
        # Upload finishes asynchronously; the submit button is disabled while processing.
        button = page.get_by_role("button", name="Submit application", exact=True)
        posting = True
        stage = "submit"
        button.click(timeout=30000)
        deadline = time.monotonic() + outcome_timeout / 1000
        notified = None
        while not confirmation(page.url, identity):
            if gate.error:
                raise PortalRejected(gate.error)
            if response_status == 429:
                raise PortalRateLimited(retry_after)
            needs_human = gate.pending or (not gate.uncertain and challenge(page))
            if needs_human:
                stage = "verification"
                if on_human_verification is None:
                    raise PortalRejected("cato_human_verification_required")
                if notified != gate.posts:
                    on_human_verification(page)
                    notified = gate.posts
            elif response_status in {400, 401, 403, 404, 413, 422, 428}:
                raise PortalRejected("cato_application_rejected")
            if time.monotonic() >= deadline:
                if gate.uncertain:
                    raise PortalUnknown("cato_submission_outcome_unknown")
                raise PortalRejected(
                    "cato_human_verification_required" if needs_human else "cato_form_needs_review"
                )
            page.wait_for_timeout(100)
        if response_status is None or not 200 <= response_status < 300:
            raise PortalUnknown("cato_confirmation_unverified")
        return f"cato_greenhouse:{identity}"
    except PortalRejected, PortalUnknown, PortalRateLimited:
        raise
    except Exception as exc:
        if gate.uncertain:
            raise PortalUnknown("cato_submission_outcome_unknown") from exc
        raise PortalRejected(f"cato_{stage}_unavailable") from exc
    finally:
        logging.getLogger(__name__).info(
            "Cato delivery diagnostics",
            extra={
                "stage": stage,
                "submission_attempted": gate.posts > 0,
                "submission_uncertain": gate.uncertain,
                "verification_pending": gate.pending,
                "post_count": gate.posts,
                "changed_field_names": gate.changed_fields,
                "submission_http_status": response_status,
            },
        )
        with suppress(Error):
            context.close()
