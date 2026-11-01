"""Real Chromium, intercepted HTTPS only. No candidate data leaves these fixtures."""

import json

import pytest

from jobs.services import cato_browser as browser
from jobs.services.secret_telaviv import PortalRateLimited, PortalRejected, PortalUnknown
from tests.test_cato_greenhouse import ANSWERS, IDENTITY, PDF

SUBMIT = f"https://boards.greenhouse.io/embed/catonetworks/jobs/{IDENTITY}"
RECEIPT = f"/embed/job_app/confirmation?for=catonetworks&token={IDENTITY}"
HTML = """<!doctype html><html><body>
<input id=first_name><input id=last_name><input id=email>
<fieldset><legend>Phone</legend><input id=country>
<button aria-label="Toggle flyout" onclick="countries.hidden=false">Country</button>
<div id=countries hidden><div role=option onclick="countries.hidden=true">Israel +972</div></div>
<input id=phone></fieldset><input id=candidate-location oninput="cities.hidden=false">
<div id=cities hidden><div role=option onclick="cities.hidden=true">Tel Aviv, Israel</div></div>
<input type=file id=resume><input id=question_123>
<button id=send>Submit application</button>
<script>
window.__remixContext={state:{loaderData:{'routes/embed.job_app':{
submitPath:SUBMIT_VALUE,confirmationPath:RECEIPT_VALUE}}}};
document.querySelector('#resume').onchange=async()=>{
 await fetch('https://grnhse-prod-jben-us-east-1.s3.amazonaws.com/',
  {method:'POST',body:'fixture CV'});
};
document.querySelector('#send').onclick=async()=>{
 const result=await fetch(SUBMIT_VALUE,{method:'POST',
 headers:{'Content-Type':'application/json'},
 body:JSON.stringify({job_application:{fixture:true}})});
 if(result.ok) location.href=RECEIPT_VALUE;
};
</script></body></html>""".replace("SUBMIT_VALUE", json.dumps(SUBMIT)).replace(
    "RECEIPT_VALUE", json.dumps(RECEIPT)
)


@pytest.mark.browser
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "hydration",
        "short_city",
        "429",
        "422",
        "timeout",
        "challenge",
        "city",
        "destination",
    ],
)
def test_browser_transport_uses_ui_and_never_replays(browser_type, outcome):
    chromium = browser_type.launch()
    context = chromium.new_context()
    requests = []
    html = HTML
    answers = ANSWERS.copy()
    if outcome == "short_city":
        answers["city"] = "Lod"
        html = html.replace("Tel Aviv, Israel", "Lod, Israel").replace(
            'oninput="cities.hidden=false"', 'oninput="cities.hidden=this.value.length<4"'
        )
    if outcome == "hydration":
        html = html.replace('onclick="countries.hidden=false"', "")
        html = html.replace(
            "<script>",
            """<script>
setTimeout(() => {
 document.querySelector('[aria-label="Toggle flyout"]').onclick=()=>countries.hidden=false;
}, 1200);
""",
        )
    if outcome == "challenge":
        html = html.replace(
            "<input id=first_name>", "<input id=first_name><input name=security_code>"
        )
    elif outcome == "city":
        html = html.replace("Tel Aviv, Israel", "Tel Aviv, United States")
    elif outcome == "destination":
        html = html.replace(
            "https://boards.greenhouse.io/embed/catonetworks/", "https://evil.example/"
        )

    def intercept(route):
        request = route.request
        if request.method == "OPTIONS":
            route.fulfill(
                status=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST, GET",
                    "Access-Control-Allow-Headers": "*",
                },
            )
        elif request.url == SUBMIT and request.method == "POST":
            requests.append("submit")
            if outcome == "timeout":
                route.abort()
            else:
                route.fulfill(
                    status=int(outcome) if outcome in {"429", "422"} else 200,
                    headers={"Access-Control-Allow-Origin": "*", "Retry-After": "120"},
                    body="{}",
                )
        elif request.url.startswith("https://" + browser.UPLOAD_HOST):
            requests.append("upload")
            route.fulfill(status=201, headers={"Access-Control-Allow-Origin": "*"}, body="uploaded")
        elif "/confirmation?" in request.url:
            route.fulfill(content_type="text/html", body="<h1>Application submitted</h1>")
        else:
            route.fulfill(content_type="text/html", body=html)

    context.route("**/*", intercept)
    try:
        if outcome in {"success", "hydration", "short_city"}:
            assert (
                browser.run(context, IDENTITY, answers, PDF, outcome_timeout=500)
                == f"cato_greenhouse:{IDENTITY}"
            )
        else:
            error = (
                PortalRateLimited
                if outcome == "429"
                else PortalUnknown
                if outcome == "timeout"
                else PortalRejected
            )
            with pytest.raises(error):
                browser.run(context, IDENTITY, ANSWERS, PDF, outcome_timeout=500)
        assert requests.count("submit") == (
            0 if outcome in {"challenge", "city", "destination"} else 1
        )
        if outcome in {"challenge", "city", "destination"}:
            assert "upload" not in requests
    finally:
        chromium.close()


def test_receipt_requires_exact_employer_and_job():
    url = "https://job-boards.greenhouse.io" + RECEIPT
    assert browser.confirmation(url, IDENTITY)
    assert not browser.confirmation(url.replace("catonetworks", "other"), IDENTITY)
    assert not browser.confirmation(url.replace(IDENTITY, "123"), IDENTITY)
    assert not browser.confirmation(url.replace(".io", ".io.evil.example"), IDENTITY)


def test_verification_post_requires_explicit_rejection_and_same_application():
    gate = browser.VerificationGate(ANSWERS["email"])
    payload = {"job_application": {"fixture": True}}
    assert gate.permit(payload)
    assert not gate.permit(payload)  # No duplicate while acceptance is uncertain.
    gate.observe(428, {"code": "captcha-failed", "security_code_recipient": ANSWERS["email"]})
    assert gate.pending and not gate.uncertain
    assert gate.permit({**payload, "security_code": "ABCD1234"})
    assert not gate.permit({**payload, "security_code": "ABCD1234"})
    gate.observe(428, {"code": "invalid-security-code"})
    assert gate.permit({**payload, "security_code": "EFGH5678"})


def test_verification_cannot_change_content_recipient_or_retry_unknown_failure():
    gate = browser.VerificationGate(ANSWERS["email"])
    payload = {"job_application": {"fixture": True}}
    assert gate.permit(payload)
    gate.observe(500, {"code": "captcha-failed", "security_code_recipient": ANSWERS["email"]})
    assert gate.uncertain and not gate.pending
    gate.observe(
        428, {"code": "captcha-failed", "security_code_recipient": "other@example.invalid"}
    )
    assert gate.error == "cato_verification_recipient_changed"
    gate = browser.VerificationGate(ANSWERS["email"])
    gate.permit(payload)
    gate.observe(428, {"code": "captcha-failed", "security_code_recipient": ANSWERS["email"]})
    assert not gate.permit({"job_application": {"changed": True}, "security_code": "ABCD1234"})
    assert gate.error == "cato_reviewed_content_changed"


def test_verification_allows_only_nonfactual_metadata_and_equivalent_formatting():
    gate = browser.VerificationGate(ANSWERS["email"])
    first = {
        "job_application": {
            "first_name": "Test ",
            "email": "Test@example.invalid",
            "phone": "+1 (202) 555-0123",
            "resume_url": "fixed-upload",
            "time_zone": "UTC",
        }
    }
    assert gate.permit(first)
    gate.observe(428, {"code": "captcha-failed", "security_code_recipient": ANSWERS["email"]})
    second = {
        "job_application": {
            "first_name": "Test",
            "email": "test@example.invalid",
            "phone": "+12025550123",
            "resume_url": "fixed-upload",
            "time_zone": "Europe/Kiev",
            "mapped_url_token": "provider-attribution",
        },
        "security_code": "ABCD1234",
    }
    assert gate.permit(second)


@pytest.mark.browser
@pytest.mark.parametrize("human", [True, False])
def test_email_verification_continuation(browser_type, human):
    chromium = browser_type.launch()
    context = chromium.new_context()
    app = {"fixture": True}
    html = HTML.replace(
        "body:JSON.stringify({job_application:{fixture:true}})",
        "body:JSON.stringify({job_application:{fixture:true},security_code:document.querySelector('#code')?.value})",
    )
    html = html.replace(
        "if(result.ok) location.href=",
        "if(result.status===428){const x=document.createElement('input');"
        "x.id='code';x.name='security_code';document.body.append(x);} "
        "if(result.ok) location.href=",
    )
    posts = []

    def intercept(route):
        r = route.request
        if r.method == "OPTIONS":
            route.fulfill(
                status=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST",
                    "Access-Control-Allow-Headers": "*",
                },
            )
        elif r.url == SUBMIT and r.method == "POST":
            payload = json.loads(r.post_data)
            posts.append(payload)
            if len(posts) == 1:
                route.fulfill(
                    status=428,
                    content_type="application/json",
                    headers={"Access-Control-Allow-Origin": "*"},
                    body=json.dumps(
                        {"code": "captcha-failed", "security_code_recipient": ANSWERS["email"]}
                    ),
                )
            else:
                assert payload["job_application"] == app and payload["security_code"] == "ABCD1234"
                route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*"}, body="{}")
        elif r.url.startswith("https://" + browser.UPLOAD_HOST):
            route.fulfill(status=201, headers={"Access-Control-Allow-Origin": "*"}, body="uploaded")
        elif "/confirmation?" in r.url:
            route.fulfill(content_type="text/html", body="<h1>Application submitted</h1>")
        else:
            route.fulfill(content_type="text/html", body=html)

    context.route("**/*", intercept)

    def notify(page):
        page.locator("#code").fill("ABCD1234")
        page.get_by_role("button", name="Submit application", exact=True).click()

    try:
        if human:
            assert (
                browser.run(
                    context,
                    IDENTITY,
                    ANSWERS,
                    PDF,
                    outcome_timeout=3000,
                    on_human_verification=notify,
                )
                == f"cato_greenhouse:{IDENTITY}"
            )
            assert len(posts) == 2
        else:
            with pytest.raises(PortalRejected, match="human_verification"):
                browser.run(context, IDENTITY, ANSWERS, PDF, outcome_timeout=1000)
            assert len(posts) == 1
    finally:
        chromium.close()
