"""Real Chromium with fully intercepted HTTPS; no applicant data leaves fixtures."""

import json

import pytest

from jobs.services import gitlab_browser as browser
from jobs.services.secret_telaviv import PortalRateLimited, PortalRejected, PortalUnknown
from tests.test_gitlab_greenhouse import IDENTITY, PDF, SCHEMA, fixture_answers

SUBMIT = f"https://boards.greenhouse.io/gitlab/jobs/{IDENTITY}"
TARGET = f"https://job-boards.greenhouse.io/gitlab/jobs/{IDENTITY}"
RECEIPT = f"/gitlab/jobs/{IDENTITY}/confirmation"


def fixture_html():
    controls = []
    for q in SCHEMA.entries:
        f = q["fields"][0]
        if f["type"] == "multi_value_single_select":
            options = "".join(
                f'<div role="option" onclick="this.parentNode.hidden=true">{v["label"]}</div>'
                for v in f["values"]
            )
            controls.append(
                f'<input id="{f["name"]}" onclick="this.nextElementSibling.hidden=false">'
                f"<div hidden>{options}</div>"
            )
        elif f["name"] == "phone":
            controls.append(
                '<fieldset><legend>Phone</legend><button aria-label="Toggle flyout" '
                'onclick="countries.hidden=!countries.hidden">Country</button>'
                '<div id="countries" hidden><div role="option" '
                'onclick="countries.hidden=true">Israel +972</div></div>'
                '<input id="phone"></fieldset>'
            )
        else:
            controls.append(f'<input id="{f["name"]}">')
    data = {
        "jobPost": {
            "questions": SCHEMA.payload["questions"],
            "education_config": {"school": "hidden"},
        },
        "submitPath": SUBMIT,
        "confirmationPath": RECEIPT,
    }
    return (
        "<html><body>"
        + "".join(controls)
        + """<input id="resume" type="file"><div id="uploaded"></div>
<button id="send">Submit application</button><script>
window.__remixContext={state:{loaderData:{ROUTE:DATA}}};
resume.onchange=async()=>{
const r=await fetch('https://grnhse-prod-jben-us-east-1.s3.amazonaws.com/',
{method:'POST',body:'SYNTHETIC CV'});
if(r.ok)uploaded.textContent='resume.pdf';};
send.onclick=async()=>{
const r=await fetch(SUBMIT,{method:'POST',
body:JSON.stringify({job_application:APPLICATION})});
if(r.ok)location.href=RECEIPT;};
</script></body></html>""".replace("ROUTE", json.dumps(browser.ROUTE))
        .replace("DATA", json.dumps(data))
        .replace("SUBMIT", json.dumps(SUBMIT))
        .replace("RECEIPT", json.dumps(RECEIPT))
        .replace(
            "APPLICATION",
            json.dumps(
                {
                    "resume_url": "https://" + browser.UPLOAD_HOST + "/synthetic/resume.pdf",
                    "resume_url_filename": "resume.pdf",
                    **{k: fixture_answers()[k] for k in ("first_name", "last_name", "email")},
                }
            ),
        )
    )


@pytest.mark.browser
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "429",
        "422",
        "verification",
        "unknown",
        "schema",
        "upload",
        "foreign",
        "duplicate",
        "missing_cv",
    ],
)
def test_isolated_ui_submission(browser_type, outcome):
    chromium = browser_type.launch()
    context = chromium.new_context()
    html = fixture_html()
    requests = []
    if outcome == "missing_cv":
        html = html.replace("resume_url_filename", "unbound_filename")
    if outcome == "schema":
        html = html.replace('"First Name"', '"Changed Name"')
    if outcome == "foreign":
        html = html.replace(
            'await fetch("https://boards.greenhouse.io', 'await fetch("https://evil.example'
        )
    if outcome == "duplicate":
        html = html.replace(
            "if(r.ok)location.href=",
            "await fetch("
            + json.dumps(SUBMIT)
            + ",{method:'POST',body:'duplicate'}).catch(()=>{});if(r.ok)location.href=",
        )

    def intercept(route):
        r = route.request
        if r.method == "OPTIONS":
            route.fulfill(
                status=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST,GET",
                    "Access-Control-Allow-Headers": "*",
                },
            )
        elif r.url == SUBMIT and r.method == "POST":
            requests.append("submit")
            if outcome == "unknown":
                route.abort()
            else:
                status = {"429": 429, "422": 422, "verification": 428}.get(outcome, 200)
                body = json.dumps({"code": "captcha-failed"}) if outcome == "verification" else "{}"
                route.fulfill(
                    status=status,
                    headers={"Access-Control-Allow-Origin": "*", "Retry-After": "60"},
                    content_type="application/json",
                    body=body,
                )
        elif r.url.startswith("https://" + browser.UPLOAD_HOST):
            requests.append("upload")
            route.fulfill(
                status=500 if outcome == "upload" else 201,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        elif "/confirmation" in r.url:
            route.fulfill(content_type="text/html", body="<h1>Confirmed</h1>")
        elif r.url == TARGET:
            route.fulfill(content_type="text/html", body=html)
        else:
            requests.append("foreign")
            route.abort()

    context.route("**/*", intercept)
    try:
        if outcome in {"success", "duplicate"}:
            assert (
                browser.run(context, IDENTITY, fixture_answers(), PDF, SCHEMA, outcome_timeout=700)
                == f"gitlab_greenhouse:{IDENTITY}"
            )
        else:
            error = (
                PortalRateLimited
                if outcome == "429"
                else PortalUnknown
                if outcome == "unknown"
                else PortalRejected
            )
            with pytest.raises(error):
                browser.run(context, IDENTITY, fixture_answers(), PDF, SCHEMA, outcome_timeout=700)
        assert requests.count("submit") == (
            0 if outcome in {"schema", "upload", "foreign", "missing_cv"} else 1
        )
        assert "foreign" not in requests
    finally:
        chromium.close()


def test_confirmation_requires_exact_official_route():
    assert browser.confirmation("https://job-boards.greenhouse.io" + RECEIPT, IDENTITY)
    for url in (
        "https://evil.example" + RECEIPT,
        TARGET,
        "https://job-boards.greenhouse.io/gitlab/jobs/1/confirmation",
    ):
        assert not browser.confirmation(url, IDENTITY)


@pytest.mark.browser
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "invalid",
        "recipient",
        "expired",
        "changed",
        "cv",
        "timeout",
        "lost",
        "challenge",
        "initial_timeout",
    ],
)
def test_assisted_verification_same_session(browser_type, outcome):
    chromium = browser_type.launch()
    context = chromium.new_context()
    html = fixture_html().replace(
        "{job_application:", "{security_code:window.securityCode,job_application:"
    )
    if outcome in {"challenge", "initial_timeout"}:
        html = html.replace("<html><body>", '<html><body><input name="security_code">')
    posts = []
    notifications = []

    def intercept(route):
        r = route.request
        headers = {"Access-Control-Allow-Origin": "*"}
        if r.method == "OPTIONS":
            route.fulfill(status=204, headers={**headers, "Access-Control-Allow-Headers": "*"})
        elif r.url == SUBMIT and r.method == "POST":
            posts.append(json.loads(r.post_data))
            if len(posts) == 1:
                code = "captcha-failed"
                recipient = (
                    "wrong@example.invalid"
                    if outcome == "recipient"
                    else fixture_answers()["email"]
                )
                body = {"code": code, "security_code_recipient": recipient}
            elif outcome == "lost":
                route.abort()
                return
            elif outcome in {"invalid", "expired"}:
                body = {
                    "code": "invalid-security-code"
                    if outcome == "invalid"
                    else "expired-security-code"
                }
            else:
                route.fulfill(status=200, headers=headers, body="{}")
                return
            route.fulfill(
                status=428, headers=headers, content_type="application/json", body=json.dumps(body)
            )
        elif r.url.startswith("https://" + browser.UPLOAD_HOST):
            route.fulfill(status=201, headers=headers)
        elif "/confirmation" in r.url:
            route.fulfill(content_type="text/html", body="Confirmed")
        elif r.url == TARGET:
            route.fulfill(content_type="text/html", body=html)
        else:
            route.abort()

    def notify(page):
        notifications.append(True)
        if outcome in {"timeout", "initial_timeout"}:
            return
        if outcome == "challenge" and not posts:
            page.locator('[name="security_code"]').evaluate("el => el.remove()")
            return
        page.evaluate("window.securityCode = 'abcd1234'")
        if outcome in {"changed", "cv"}:
            key = "email" if outcome == "changed" else "resume_url"
            page.evaluate(
                "([key,value]) => {const original=window.fetch; window.fetch=(u,o)=>{"
                "if(u.includes('boards.greenhouse.io/gitlab')){const p=JSON.parse(o.body);"
                "p.job_application[key]=value;o.body=JSON.stringify(p);}return original(u,o);};}",
                [
                    key,
                    "changed@example.invalid"
                    if outcome == "changed"
                    else "https://" + browser.UPLOAD_HOST + "/other.pdf",
                ],
            )
        page.locator("#send").click()

    context.route("**/*", intercept)
    try:
        args = dict(on_human_verification=notify, assisted_timeout=2, outcome_timeout=500)
        if outcome in {"success", "challenge"}:
            assert browser.run(context, IDENTITY, fixture_answers(), PDF, SCHEMA, **args).endswith(
                IDENTITY
            )
        else:
            with pytest.raises(PortalUnknown if outcome == "lost" else PortalRejected):
                browser.run(context, IDENTITY, fixture_answers(), PDF, SCHEMA, **args)
        expected_posts = {
            "invalid": 4,
            "initial_timeout": 0,
            "success": 2,
            "challenge": 2,
            "expired": 2,
            "lost": 2,
        }.get(outcome, 1)
        assert len(posts) == expected_posts
        assert len(notifications) <= 3
    finally:
        chromium.close()


@pytest.mark.parametrize("started", [False, True])
def test_assisted_visible_launch_and_browser_failure(monkeypatch, started):
    from unittest.mock import Mock

    runtime = Mock()
    runtime.__enter__ = Mock(return_value=runtime)
    runtime.__exit__ = Mock(return_value=False)
    chromium = Mock()
    launch = Mock(return_value=chromium, side_effect=None if started else OSError())
    notify = Mock()
    monkeypatch.setattr(browser, "sync_playwright", lambda: runtime)
    monkeypatch.setattr(browser, "resolver_rules", lambda: "pinned")
    monkeypatch.setattr(browser, "launch", launch)
    monkeypatch.setattr(browser, "run", Mock(side_effect=OSError()))
    with pytest.raises(PortalUnknown if started else PortalRejected):
        browser.deliver(
            IDENTITY, fixture_answers(), PDF, SCHEMA, assisted=True, on_human_verification=notify
        )
    launch.assert_called_once_with(runtime, "pinned", headless=False)
    if started:
        chromium.close.assert_called_once()
