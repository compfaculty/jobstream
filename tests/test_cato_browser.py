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
 const result=await fetch(SUBMIT_VALUE,{method:'POST',body:'fixture application'});
 if(result.ok) location.href=RECEIPT_VALUE;
};
</script></body></html>""".replace("SUBMIT_VALUE", json.dumps(SUBMIT)).replace(
    "RECEIPT_VALUE", json.dumps(RECEIPT)
)


@pytest.mark.browser
@pytest.mark.parametrize(
    "outcome", ["success", "hydration", "429", "422", "timeout", "challenge", "city", "destination"]
)
def test_browser_transport_uses_ui_and_never_replays(browser_type, outcome):
    chromium = browser_type.launch()
    context = chromium.new_context()
    requests = []
    html = HTML
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
        if outcome in {"success", "hydration"}:
            assert (
                browser.run(context, IDENTITY, ANSWERS, PDF, outcome_timeout=500)
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
