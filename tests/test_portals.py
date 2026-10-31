import pytest

from jobs.services.portals import inspect_secret, secret_url

URL = "https://jobs.secrettelaviv.com/job/test-vacancy/"
FORM = b"""<form id="wpjb-apply-form" method="post" action="?form=apply#wpjb-scroll">
<input name="_wpjb_action" type="hidden" value="not-logged">
<input name="_job_id" type="hidden" value="123">
<input name="applicant_name" type="text"><input name="applicant_surname" type="text">
<input name="email" type="text"><input name="telephone_number_apply_form" type="text">
<div class="wpjb-element-name-similar_employers_cv">
<input type="checkbox" name="similar_employers_cv[]">
<label>I agree to
<a href="https://www.secrettelaviv.com/terms/">terms and conditions</a></label></div>
<div class="wpjb-element-name-headhunters">
<input type="checkbox" name="headhunters[]"><label>Forward to headhunters</label></div>
<div id="wpjb-upload-container-file"><a id="wpjb-upload-browse-file">Browse</a></div>
<input type="submit"></form>"""


def test_preflight_recognizes_but_never_enables_submission():
    result = inspect_secret(FORM, URL)
    assert not result.issues
    assert result.requires_javascript_upload
    assert not result.submission_enabled
    assert result.form_url == URL + "?form=apply"
    assert result.terms_url == "https://www.secrettelaviv.com/terms/"
    assert "not-logged" not in str(result.as_dict())
    assert (
        result.fingerprint
        == inspect_secret(FORM.replace(b"not-logged", b"new-token"), URL).fingerprint
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://jobs.secrettelaviv.com/job/test/",
        "https://jobs.secrettelaviv.com.attacker.example/job/test/",
        "https://jobs.secrettelaviv.com/account/login/",
        "https://jobs.secrettelaviv.com/job/test/?access_token=not-logged",
    ],
)
def test_portal_scope_is_explicit(url):
    with pytest.raises(ValueError):
        secret_url(url)


def test_changed_fields_action_terms_and_challenge_are_reported():
    data = (
        FORM.replace(b'name="email"', b'name="new_required_field"')
        .replace(b'action="?form=apply#wpjb-scroll"', b'action="https://elsewhere.example/upload"')
        .replace(b"https://www.secrettelaviv.com/terms/", b"https://elsewhere.example/terms/")
        .replace(b"</form>", b'<div class="g-recaptcha"></div></form>')
    )
    result = inspect_secret(data, URL)
    assert set(result.issues) >= {
        "application_action_changed",
        "application_fields_changed",
        "consent_destination_changed",
        "user_challenge_required",
    }
    assert not result.submission_enabled


def test_missing_or_duplicate_form_is_not_recognized():
    assert inspect_secret(b"<html>Sign in</html>", URL).issues
    assert inspect_secret(FORM + FORM, URL).issues


def test_preselected_optional_consent_is_reported():
    data = FORM.replace(b'name="headhunters[]"', b'name="headhunters[]" checked')
    assert "consent_preselected" in inspect_secret(data, URL).issues
