"""Camunda's normal Ashby UI; one final submission, explicit GraphQL receipt only."""

import json
import logging
import time
from contextlib import closing, suppress
from urllib.parse import urlsplit

from playwright.sync_api import Error, sync_playwright

from .camunda_ashby import inspect_schema
from .cato_browser import challenge, launch
from .network import PinnedHTTPSConnection, public_addresses
from .secret_telaviv import PortalRateLimited, PortalRejected, PortalUnknown

HOSTS = frozenset(
    {
        "jobs.ashbyhq.com",
        "app.ashbyhq.com",
        "cdn.ashbyprd.com",
        "www.recaptcha.net",
        "www.gstatic.com",
        "www.google.com",
    }
)
READ_OPS = {"ApiJobPosting", "ApiOrganizationFromHostedJobsPageName", "ApiAutocompleteGeoLocation"}
WRITE_OPS = {
    "ApiSetFormValue",
    "ApiSetFormValueToFile",
    "ApiCreateFileUploadHandle",
    "ApiSubmitCandidateTextingConsent",
}
SUBMIT_OP = "ApiSubmitMultipleFormsAction"
SINGLE_SUBMIT_OP = "ApiSubmitSingleApplicationFormAction"


def operation(request):
    if request.method != "POST" or urlsplit(request.url).path != "/api/non-user-graphql":
        return ""
    try:
        name = json.loads(request.post_data or "{}").get("operationName", "")
        return name if isinstance(name, str) else ""
    except ValueError, AttributeError:
        return ""


def field_container(page, entry):
    return page.locator(f'[data-field-entry-id="{entry["id"]}"]')


def location_label(labels, value, country_hint=""):
    def fold(text):
        return " ".join(text.casefold().replace("-", " ").split())

    exact = [label for label in labels if fold(label) == fold(value)]
    if len(exact) == 1:
        return exact[0]
    matches = [
        label
        for label in labels
        if fold(label.split(",")[0]) == fold(value)
        and (not country_hint or fold(label.split(",")[-1]) == fold(country_hint))
    ]
    if len(matches) != 1:
        raise PortalRejected("camunda_location_needs_review")
    return matches[0]


def fill_entry(page, entry, value, country_hint=""):
    field = entry["field"]
    container, kind = field_container(page, entry), field["type"]
    if kind in {"String", "Email", "Phone"}:
        container.get_by_role("textbox").fill(value)
    elif kind == "Boolean":
        button = container.get_by_role("button", name="Yes" if value == "yes" else "No", exact=True)
        button.click()
        if button.get_attribute("aria-pressed") != "true":
            raise PortalRejected("camunda_form_not_interactive")
    elif kind == "MultiValueSelect":
        for choice in field["selectableValues"]:
            container.get_by_role("checkbox", name=choice["label"], exact=True).set_checked(
                choice["value"] in value
            )
    elif kind == "ValueSelect" and container.get_by_role("radio").count():
        label = next(c["label"] for c in field["selectableValues"] if c["value"] == value)
        container.get_by_role("radio", name=label, exact=True).check()
    elif kind in {"ValueSelect", "Location"}:
        label = (
            value
            if kind == "Location"
            else next(c["label"] for c in field["selectableValues"] if c["value"] == value)
        )
        container.get_by_role("combobox").fill(label)
        if kind == "Location":
            page.get_by_role("option").first.wait_for(state="visible")
            label = location_label(
                page.get_by_role("option").all_text_contents(), value, country_hint
            )
        option = page.get_by_role("option", name=label, exact=True)
        option.first.wait_for(state="visible")
        if option.count() != 1:
            raise PortalRejected("camunda_selection_ambiguous")
        option.click()


def pinned_upload(route, url):
    """Only the upload URL endorsed by Ashby's response. Never redirect/replay."""
    parts = urlsplit(url)
    host = parts.hostname
    if (
        parts.scheme != "https"
        or not host
        or not host.endswith(".amazonaws.com")
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
    ):
        raise PortalRejected("camunda_upload_destination_changed")
    body = route.request.post_data_buffer
    if not body or len(body) > 6 * 1024 * 1024:
        raise PortalRejected("camunda_upload_invalid")
    headers = {
        key: value
        for key, value in route.request.headers.items()
        if key.lower()
        in {
            "content-type",
            "origin",
            "accept",
        }
    }
    with closing(PinnedHTTPSConnection(host, public_addresses(host)[0])) as connection:
        connection.request(
            "POST",
            parts.path + ("?" + parts.query if parts.query else ""),
            body=body,
            headers=headers,
        )
        response = connection.getresponse()
        content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise PortalRejected("camunda_upload_response_invalid")
        route.fulfill(status=response.status, headers=dict(response.getheaders()), body=content)
        return response.status, response.getheader("retry-after", "")


def upload_target(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not parts.hostname.endswith(".amazonaws.com")
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise PortalRejected("camunda_upload_destination_changed")
    # Browser fetch canonicalizes a bare origin to a slash-terminated URL.
    return parts._replace(path=parts.path or "/").geturl()


def upload_handle_response(request):
    """Relay this browser request so the upload destination is known before JS gets it.

    A normal response callback races with the frontend starting its S3 upload.
    This is the same public UI request, with its cookies, not an additional API call.
    """
    parts = urlsplit(request.url)
    headers = {
        k: v
        for k, v in request.all_headers().items()
        if k.lower()
        in {
            "cookie",
            "content-type",
            "origin",
            "referer",
            "user-agent",
            "accept",
        }
    }
    host = "jobs.ashbyhq.com"
    headers["accept-encoding"] = "identity"
    with closing(PinnedHTTPSConnection(host, public_addresses(host)[0])) as connection:
        connection.request(
            "POST", parts.path + "?" + parts.query, body=request.post_data_buffer, headers=headers
        )
        response = connection.getresponse()
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise PortalRejected("camunda_upload_handle_invalid")
        headers = {}
        for key, value in response.getheaders():
            name = key.lower()
            if name not in {"content-length", "transfer-encoding", "content-encoding"}:
                headers[name] = headers[name] + "\n" + value if name in headers else value
        return response.status, headers, body


def run(
    context, identity, answers, attachment, schema, *, timeout=45000, on_human_verification=None
):
    target = f"https://jobs.ashbyhq.com/camunda/{identity}/application"
    submit_op = SUBMIT_OP if schema.payload["surveyForms"] else SINGLE_SUBMIT_OP
    attempted, sending, writing = False, False, False
    observed_schema = None
    pending = set()
    failure = ""
    rate_limit = None
    receipt = False
    upload_url = ""
    upload_ok = False
    resume_bound = False
    submission_status = None
    rejected = False
    step = "navigation"
    blocked_hosts = set()
    blocked_operations = set()
    human_notified = False

    def guard(route):
        nonlocal attempted, failure, rate_limit, upload_ok, upload_url
        request = route.request
        parts = urlsplit(request.url)
        if request.url == upload_url and request.method == "POST" and writing:
            try:
                status, retry = pinned_upload(route, upload_url)
                upload_ok = 200 <= status < 300
                if status == 429:
                    rate_limit = retry
                elif not upload_ok:
                    failure = "camunda_upload_failed"
            except Exception:
                failure = "camunda_upload_failed"
                route.abort()
            return
        allowed = (
            parts.scheme == "https"
            and parts.hostname in HOSTS
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
        )
        if request.method == "POST" and parts.hostname == "jobs.ashbyhq.com":
            op = operation(request)
            try:
                variables = json.loads(request.post_data or "{}").get("variables", {})
                scoped = variables.get("organizationHostedJobsPageName", "camunda") == "camunda"
                if op == submit_op:
                    scoped = scoped and variables.get("jobPostingId") == identity
            except ValueError, AttributeError:
                scoped = False
            allowed = (
                allowed
                and (
                    op in READ_OPS
                    or (writing and op in WRITE_OPS and rate_limit is None and not failure)
                    or (
                        sending
                        and op == submit_op
                        and not attempted
                        and rate_limit is None
                        and not failure
                    )
                )
                and scoped
            )
            if allowed:
                if op == submit_op:
                    attempted = True
                elif op in WRITE_OPS:
                    pending.add(request)
                if op == "ApiCreateFileUploadHandle":
                    try:
                        status, headers, body = upload_handle_response(request)
                        if status == 200:
                            upload_url = upload_target(
                                json.loads(body)["data"]["fileUploadHandle"]["url"]
                            )
                        route.fulfill(status=status, headers=headers, body=body)
                    except Exception:
                        failure = "camunda_upload_handle_failed"
                        pending.discard(request)
                        route.abort()
                    return
        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
            allowed = allowed and parts.hostname in {"www.recaptcha.net", "www.google.com"}
        if allowed:
            route.fallback()
        else:
            blocked_hosts.add(parts.hostname or "missing")
            if operation(request):
                blocked_operations.add(operation(request))
            route.abort()

    def on_response(response):
        nonlocal observed_schema, failure, receipt, rate_limit, upload_url, resume_bound
        nonlocal submission_status, rejected
        op = operation(response.request)
        if op not in READ_OPS | WRITE_OPS | {submit_op}:
            return
        if op == submit_op:
            submission_status = response.status
        if response.status == 429:
            rate_limit = response.headers.get("retry-after", "")
        elif response.status != 200:
            failure = (
                "camunda_provider_rejected"
                if response.status in {400, 401, 403, 404, 413, 422}
                else "camunda_provider_unavailable"
            )
            rejected = op == submit_op and response.status in {400, 401, 403, 404, 413, 422}
        else:
            try:
                payload = response.json()
                if payload.get("errors"):
                    failure = "camunda_form_rejected"
                elif op == "ApiJobPosting":
                    observed_schema = inspect_schema(payload["data"]["jobPosting"], identity)
                elif op == "ApiCreateFileUploadHandle":
                    upload_url = upload_target(payload["data"]["fileUploadHandle"]["url"])
                elif op == "ApiSetFormValueToFile":
                    resume_bound = True
                elif op == submit_op:
                    key = (
                        "submitMultipleFormsAction"
                        if submit_op == SUBMIT_OP
                        else "submitApplicationFormAction"
                    )
                    submission = payload["data"][key]
                    result = submission["applicationFormResult"]
                    if (submission.get("messages") or {}).get("blockMessageForCandidateHtml"):
                        failure = "camunda_employer_block_requires_review"
                    elif result["__typename"] == "FormSubmitSuccess":
                        surveys = submission.get("surveyFormResults", [])
                        if len(surveys) == len(schema.payload["surveyForms"]) and all(
                            item["__typename"] == "FormSubmitSuccess" for item in surveys
                        ):
                            receipt = True
                        else:
                            failure = "camunda_survey_outcome_requires_review"
                    elif result["__typename"] in {"FormRender", "FormSubmitError"}:
                        failure = "camunda_form_rejected"
                        rejected = result["__typename"] == "FormRender"
                    else:
                        failure = "camunda_unrecognized_receipt"
            except Exception:
                failure = "camunda_response_unrecognized"

    context.route("**/*", guard)
    page = context.new_page()
    page.set_default_timeout(15000)
    page.on("response", on_response)
    page.on("requestfinished", lambda r: pending.discard(r))

    def failed(request):
        nonlocal failure
        if request in pending:
            failure = "camunda_answer_save_failed"
            pending.discard(request)

    page.on("requestfailed", failed)

    def wait_until(predicate, milliseconds=timeout):
        nonlocal human_notified
        deadline = time.monotonic() + milliseconds / 1000
        while True:
            if rate_limit is not None:
                if attempted and submission_status != 429:
                    raise PortalUnknown("camunda_submission_outcome_unknown")
                raise PortalRateLimited(rate_limit)
            if failure:
                raise (
                    PortalUnknown(failure)
                    if attempted and not rejected
                    else PortalRejected(failure)
                )
            human_required = not attempted and challenge(page)
            if human_required:
                if on_human_verification is None:
                    raise PortalRejected("camunda_human_verification_required")
                if not human_notified:
                    on_human_verification(page)
                    human_notified = True
            elif predicate():
                return
            if time.monotonic() >= deadline:
                raise (
                    PortalUnknown("camunda_submission_outcome_unknown")
                    if attempted
                    else PortalRejected(f"camunda_{step}_timeout")
                )
            page.wait_for_timeout(100)

    try:
        response = page.goto(target, wait_until="domcontentloaded", timeout=30000)
        if response and response.status == 429:
            raise PortalRateLimited(response.headers.get("retry-after", ""))
        wait_until(lambda: observed_schema is not None)
        if observed_schema.fingerprint != schema.fingerprint:
            raise PortalRejected("camunda_form_changed")
        if challenge(page):
            raise PortalRejected("camunda_human_verification_required")
        writing = True
        country_hint = next(
            (
                answers.get(e["field"]["path"], "")
                for e in observed_schema.entries
                if e["field"].get("title", "").casefold() == "country"
            ),
            "",
        )
        for entry in observed_schema.entries:
            kind, path = entry["field"]["type"], entry["field"]["path"]
            if kind != "File" and answers.get(path):
                step = "field_" + path
                fill_entry(page, entry, answers[path], country_hint)
                wait_until(lambda: True)
        if answers.get("sms_consent"):
            step = "sms_consent"
            name = (
                "Yes - I consent to receiving text messages"
                if answers["sms_consent"] == "yes"
                else "No - I do not consent to receiving text messages"
            )
            page.get_by_role("radio", name=name, exact=True).check()
        page.get_by_role("button", name="Submit Application", exact=True).focus()
        wait_until(lambda: not pending)
        file_entry = next(e for e in observed_schema.entries if e["field"]["type"] == "File")
        step = "resume_upload"
        field_container(page, file_entry).locator('input[type="file"]').set_input_files(
            {
                "name": "resume.pdf",
                "mimeType": "application/pdf",
                "buffer": attachment,
            }
        )
        wait_until(lambda: upload_ok and resume_bound and not pending)
        if challenge(page):
            raise PortalRejected("camunda_human_verification_required")
        sending = True
        step = "submit"
        page.get_by_role("button", name="Submit Application", exact=True).click()
        wait_until(lambda: receipt)
        return f"camunda_ashby:{identity}"
    except PortalRejected, PortalUnknown, PortalRateLimited:
        raise
    except Exception as exc:
        if attempted:
            raise PortalUnknown("camunda_submission_outcome_unknown") from exc
        raise PortalRejected(f"camunda_{step}_unavailable") from exc
    finally:
        logging.getLogger(__name__).info(
            "Camunda delivery diagnostics",
            extra={
                "stage": step,
                "submission_attempted": attempted,
                "upload_ok": upload_ok,
                "resume_bound": resume_bound,
                "pending_operations": [operation(r) for r in pending],
                "provider_failure": failure,
                "schema_observed": observed_schema is not None,
                "blocked_hosts": sorted(blocked_hosts),
                "blocked_operations": sorted(blocked_operations),
                "challenge_visible": challenge(page),
            },
        )
        with suppress(Error):
            context.close()


def resolver_rules():
    return ", ".join(
        [f"MAP {h} {public_addresses(h)[0]}" for h in sorted(HOSTS)] + ["MAP * ~NOTFOUND"]
    )


def probe(identity, schema):
    """Read-only: inspect schema and public city suggestions; all form writes blocked."""
    with sync_playwright() as runtime:
        browser = launch(runtime, resolver_rules())
        try:
            context = browser.new_context(service_workers="block", accept_downloads=False)
            observed = None

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
                allowed = allowed and (
                    request.method in {"GET", "HEAD", "OPTIONS"}
                    or (parts.hostname == "jobs.ashbyhq.com" and operation(request) in READ_OPS)
                )
                route.continue_() if allowed else route.abort()

            def received(response):
                nonlocal observed
                if operation(response.request) == "ApiJobPosting" and response.status == 200:
                    observed = inspect_schema(response.json()["data"]["jobPosting"], identity)

            context.route("**/*", guard)
            page = context.new_page()
            page.on("response", received)
            page.goto(
                f"https://jobs.ashbyhq.com/camunda/{identity}/application",
                wait_until="domcontentloaded",
            )
            deadline = time.monotonic() + 30
            while observed is None and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            if observed is None or observed.fingerprint != schema.fingerprint:
                raise PortalRejected("camunda_form_changed")
            location = next(
                e for e in observed.entries if e["field"]["path"] == "_systemfield_location"
            )
            field_container(page, location).get_by_role("combobox").fill("Tel Aviv")
            page.get_by_role("option").first.wait_for(state="visible", timeout=15000)
            return {
                "schema_matches": True,
                "field_count": len(observed.entries),
                "form_interactive": True,
                "city_suggestions": page.get_by_role("option").all_text_contents(),
                "human_challenge": challenge(page),
            }
        finally:
            with suppress(Error):
                browser.close()


def deliver(identity, answers, attachment, schema):
    started = False
    try:
        rules = resolver_rules()
        with sync_playwright() as runtime:
            browser = launch(runtime, rules)
            try:
                context = browser.new_context(service_workers="block", accept_downloads=False)
                started = True
                return run(context, identity, answers, attachment, schema)
            finally:
                with suppress(Error):
                    browser.close()
    except (Error, OSError) as exc:
        if started:
            raise PortalUnknown("camunda_browser_outcome_unknown") from exc
        raise PortalRejected("camunda_browser_unavailable") from exc
