"""Intercepted Chromium form workflow; synthetic answers never reach the internet."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jobs.services import ashby_browser as ashby
from jobs.services.secret_telaviv import PortalRateLimited, PortalRejected, PortalUnknown
from tests.test_camunda_ashby import IDENTITY, PAYLOAD, PDF, SCHEMA, fixture_answers

HTML = """<!doctype html><html><body><main id=fields></main><script>
const schema=SCHEMA_VALUE;
async function call(op,variables={}) {
 return fetch('/api/non-user-graphql?op='+op,{method:'POST',
 headers:{'Content-Type':'application/json'},
 body:JSON.stringify({operationName:op,variables})});
}
function save(){return call('ApiSetFormValue');}
for(const form of [schema.applicationForm,...schema.surveyForms]){
 for(const section of form.sections){ for(const entry of section.fieldEntries){
 const field=entry.field, wrapper=document.createElement('div');
 wrapper.dataset.fieldEntryId=entry.id;
 fields.append(wrapper);
 if(field.type==='Boolean'){
  for(const value of ['Yes','No']){
   const b=document.createElement('button'); b.textContent=value;
   b.setAttribute('aria-pressed','false');
   b.onclick=()=>{b.setAttribute('aria-pressed','true');save();};wrapper.append(b);
  }
 } else if(field.type==='ValueSelect'){
  for(const choice of field.selectableValues){
   const label=document.createElement('label'),r=document.createElement('input');
   r.type='radio';r.name=field.path;r.onchange=save;
   label.append(r,document.createTextNode(choice.label));wrapper.append(label);
  }
 } else if(field.type==='MultiValueSelect'){
  for(const choice of field.selectableValues){
   const label=document.createElement('label'),r=document.createElement('input');
   r.type='checkbox';r.onchange=save;
   label.append(r,document.createTextNode(choice.label));wrapper.append(label);
  }
 } else if(field.type==='Location'){
  const input=document.createElement('input'),options=document.createElement('div');
  input.setAttribute('role','combobox');options.hidden=true;
  const option=document.createElement('div');option.setAttribute('role','option');
  option.textContent='Tel Aviv, Israel';
  option.onclick=()=>{options.hidden=true;input.value=option.textContent;save();};
  options.append(option);wrapper.append(input,options);
  input.oninput=()=>{options.hidden=false;};
 } else if(field.type==='File'){
  const input=document.createElement('input');input.type='file';wrapper.append(input);
  input.onchange=async()=>{
   const r=await call('ApiCreateFileUploadHandle'); const v=await r.json();
   await fetch(v.data.fileUploadHandle.url,{method:'POST',body:'SYNTHETIC FILE'});
   await call('ApiSetFormValueToFile');
  };
 } else {
  const input=document.createElement('input');input.oninput=save;wrapper.append(input);
 }
 } }
}
for(const text of ['Yes - I consent to receiving text messages',
 'No - I do not consent to receiving text messages']){
 const label=document.createElement('label'),r=document.createElement('input');r.type='radio';
 r.name='sms';r.onchange=()=>call('ApiSubmitCandidateTextingConsent');
 label.append(r,document.createTextNode(text));fields.append(label);
}
const button=document.createElement('button');button.textContent='Submit Application';
button.onclick=()=>call('ApiSubmitMultipleFormsAction',
 {organizationHostedJobsPageName:'camunda',jobPostingId:schema.id});fields.append(button);
call('ApiJobPosting');
</script></body></html>""".replace("SCHEMA_VALUE", json.dumps(PAYLOAD))


@pytest.mark.browser
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "nested_sms",
        "assisted",
        "assisted_after_upload",
        "assisted_changed",
        "assisted_expired",
        "429",
        "rejected",
        "timeout",
        "malformed",
        "employer_block",
        "survey_failed",
        "changed",
        "save_failed",
        "answer_429",
        "upload_failed",
    ],
)
def test_complete_ui_workflow(browser_type, monkeypatch, outcome):
    browser = browser_type.launch()
    context = browser.new_context()
    posts = []
    html = HTML
    answers = fixture_answers()
    if outcome == "nested_sms":
        phone = next(e for e in SCHEMA.entries if e["field"]["type"] == "Phone")
        answers[phone["field"]["path"]] = "+12025550123"
        script = f'''<script>
const phone=document.querySelector('[data-field-entry-id="{phone["id"]}"]');
for(const radio of document.querySelectorAll('input[name=sms]'))phone.append(radio.parentElement);
</script>'''
        html = html.replace("</body>", script + "</body>")
    if outcome == "assisted_after_upload":
        html = html.replace(
            "await call('ApiSetFormValueToFile');",
            """await call('ApiSetFormValueToFile');
            const c=document.createElement('input');c.name='security_code';fields.append(c);""",
        )
    if outcome in {"assisted", "assisted_changed", "assisted_expired"}:
        html = html.replace("</body>", '<input name="security_code"></body>')
    if outcome == "changed":
        html = HTML.replace('"isListed": true', '"isListed": false')

    def upload(route, url):
        posts.append("upload")
        status = 403 if outcome == "upload_failed" else 201
        route.fulfill(status=status, headers={"Access-Control-Allow-Origin": "*"}, body="fixture")
        return status, ""

    monkeypatch.setattr(ashby, "pinned_upload", upload)

    def handle(request):
        posts.append("ApiCreateFileUploadHandle")
        return (
            200,
            {"content-type": "application/json"},
            json.dumps(
                {"data": {"fileUploadHandle": {"url": "https://fixture.s3.amazonaws.com"}}}
            ).encode(),
        )

    monkeypatch.setattr(ashby, "upload_handle_response", handle)

    def intercept(route):
        req = route.request
        if req.method == "GET":
            route.fulfill(content_type="text/html", body=html)
            return
        op = json.loads(req.post_data)["operationName"]
        posts.append(op)
        if op == "ApiJobPosting":
            value = PAYLOAD if outcome != "changed" else {**PAYLOAD, "isListed": False}
            payload = {"data": {"jobPosting": value}}
        elif op == "ApiCreateFileUploadHandle":
            payload = {"data": {"fileUploadHandle": {"url": "https://fixture.s3.amazonaws.com/"}}}
        elif op == ashby.SUBMIT_OP:
            if outcome == "timeout":
                route.abort()
                return
            if outcome == "429":
                route.fulfill(status=429, headers={"Retry-After": "120"}, body="limited")
                return
            if outcome == "malformed":
                route.fulfill(content_type="application/json", body="invalid")
                return
            payload = {
                "data": {
                    "submitMultipleFormsAction": {
                        "applicationFormResult": {
                            "__typename": "FormRender"
                            if outcome == "rejected"
                            else "FormSubmitSuccess",
                        }
                    }
                }
            }
            payload["data"]["submitMultipleFormsAction"]["surveyFormResults"] = [
                {"__typename": "FormRender" if outcome == "survey_failed" else "FormSubmitSuccess"}
                for _ in PAYLOAD["surveyForms"]
            ]
            if outcome == "employer_block":
                payload["data"]["submitMultipleFormsAction"]["messages"] = {
                    "blockMessageForCandidateHtml": "Synthetic application limit block"
                }
        elif outcome == "save_failed" and op == "ApiSetFormValue":
            payload = {"errors": [{"message": "fixture rejection"}]}
        elif outcome == "answer_429" and op == "ApiSetFormValue":
            route.fulfill(status=429, headers={"Retry-After": "120"}, body="limited")
            return
        else:
            payload = {"data": {}}
        route.fulfill(content_type="application/json", body=json.dumps(payload))

    context.route("**/*", intercept)

    def human(page):
        page.locator('[name="security_code"]').evaluate("node => node.remove()")
        if outcome == "assisted_changed":
            page.locator("#fields input").first.fill("Changed without approval")

    options = {"on_human_verification": human} if outcome.startswith("assisted") else {}
    if outcome == "assisted_expired":
        options["assisted_timeout"] = 0
    try:
        if outcome in {"success", "nested_sms", "assisted", "assisted_after_upload"}:
            assert (
                ashby.run(context, IDENTITY, answers, PDF, SCHEMA, timeout=1500, **options)
                == f"camunda_ashby:{IDENTITY}"
            )
        else:
            expected = (
                PortalRateLimited
                if outcome in {"429", "answer_429"}
                else PortalUnknown
                if outcome in {"timeout", "malformed", "employer_block", "survey_failed"}
                else PortalRejected
            )
            with pytest.raises(expected):
                ashby.run(
                    context, IDENTITY, fixture_answers(), PDF, SCHEMA, timeout=1500, **options
                )
        assert posts.count(ashby.SUBMIT_OP) == (
            0
            if outcome
            in {
                "changed",
                "save_failed",
                "upload_failed",
                "answer_429",
                "assisted_changed",
                "assisted_expired",
            }
            else 1
        )
        if outcome in {"changed", "save_failed", "answer_429"}:
            assert "upload" not in posts
        if outcome == "success":
            assert (
                posts.index("upload")
                < posts.index("ApiSetFormValueToFile")
                < posts.index(ashby.SUBMIT_OP)
            )
    finally:
        browser.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://bucket.s3.amazonaws.com/",
        "https://127.0.0.1/",
        "https://bucket.s3.amazonaws.com.evil.example/",
        "https://user@bucket.s3.amazonaws.com/",
    ],
)
def test_upload_destination_is_restricted(url):
    with pytest.raises(PortalRejected):
        ashby.pinned_upload(Mock(), url)


def test_upload_pins_dns_and_never_follows_redirect(monkeypatch):
    response = Mock(status=302)
    response.read.return_value = b"redirect"
    response.getheaders.return_value = [("location", "https://evil.example/")]
    response.getheader.return_value = ""
    connection = Mock()
    connection.getresponse.return_value = response
    constructor = Mock(return_value=connection)
    monkeypatch.setattr(ashby, "PinnedHTTPSConnection", constructor)
    monkeypatch.setattr(ashby, "public_addresses", lambda host: ["203.0.113.1"])
    route = Mock(
        request=SimpleNamespace(
            post_data_buffer=b"synthetic multipart",
            headers={
                "content-type": "multipart/form-data; boundary=fixture",
                "cookie": "do-not-forward",
            },
        )
    )
    assert ashby.pinned_upload(route, "https://fixture.s3.amazonaws.com/") == (302, "")
    constructor.assert_called_once_with("fixture.s3.amazonaws.com", "203.0.113.1")
    connection.request.assert_called_once()
    assert "cookie" not in connection.request.call_args.kwargs["headers"]
    connection.close.assert_called_once()


def test_short_city_resolution_is_unique_and_country_bound():
    labels = ["Tel Aviv, Tel Aviv District, Israel", "Tel Aviv District, Israel"]
    assert ashby.location_label(labels, "Tel-Aviv", "Israel") == labels[0]
    assert ashby.location_label(labels, labels[0]) == labels[0]
    with pytest.raises(PortalRejected):
        ashby.location_label(labels, "Tel Aviv", "Canada")
    with pytest.raises(PortalRejected):
        ashby.location_label(
            ["Springfield, Illinois, United States", "Springfield, Ontario, Canada"], "Springfield"
        )
