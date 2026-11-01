"""Isolated Chromium UI transport. No API credentials, persisted session, or CAPTCHA bypass."""

import re
from contextlib import suppress
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
    location.fill(answers["city"])
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


def run(context, identity, answers, attachment, linkedin_field="", *, outcome_timeout=45000):
    """One context per attempt. Exposed separately for fully intercepted browser fixtures."""
    submit_url = f"https://boards.greenhouse.io/embed/catonetworks/jobs/{identity}"
    target = f"https://job-boards.greenhouse.io/embed/job_app?for=catonetworks&token={identity}"
    attempted = False
    posting = False
    response_status = None
    retry_after = ""
    stage = "navigation"

    def guard(route):
        nonlocal attempted
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
                (request.url == submit_url and posting and not attempted)
                or parts.hostname == UPLOAD_HOST
                or parts.hostname in {"www.recaptcha.net", "www.google.com"}
            )
        if request.method == "POST" and request.url == submit_url and allowed:
            attempted = True
        if not allowed:
            route.abort()
        else:
            route.fallback()

    def response_received(response):
        nonlocal response_status, retry_after
        if response.url == submit_url and response.request.method == "POST":
            response_status = response.status
            retry_after = response.headers.get("retry-after", "")

    context.route("**/*", guard)
    page = context.new_page()
    page.set_default_timeout(15000)
    page.on("response", response_received)
    try:
        navigation = page.goto(target, wait_until="domcontentloaded", timeout=30000)
        if navigation and navigation.status == 429:
            raise PortalRateLimited(navigation.headers.get("retry-after", ""))
        if not navigation or navigation.status != 200:
            raise PortalRejected("cato_form_unavailable")
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
        try:
            page.wait_for_url(lambda url: confirmation(str(url), identity), timeout=outcome_timeout)
        except TimeoutError:
            if response_status == 429:
                raise PortalRateLimited(retry_after) from None
            if response_status in {400, 401, 403, 404, 413, 422}:
                raise PortalRejected("cato_application_rejected") from None
            if not attempted:
                code = (
                    "cato_human_verification_required"
                    if challenge(page)
                    else "cato_form_needs_review"
                )
                raise PortalRejected(code) from None
            raise PortalUnknown("cato_submission_outcome_unknown") from None
        if response_status is None or not 200 <= response_status < 300:
            raise PortalUnknown("cato_confirmation_unverified")
        return f"cato_greenhouse:{identity}"
    except (Error, OSError) as exc:
        if attempted:
            raise PortalUnknown("cato_submission_outcome_unknown") from exc
        raise PortalRejected(f"cato_{stage}_unavailable") from exc
    finally:
        with suppress(Error):
            context.close()
