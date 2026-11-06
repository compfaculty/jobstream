"""Read-only portal preflight. Recognition is never authorization to submit."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .network import canonical_url, fetch_public_document

# Public portal hostname, not a credential.
SECRET_HOST = "jobs.secrettelaviv.com"  # noqa: S105
EXPECTED_FIELDS = {
    "_wpjb_action": "hidden",
    "_job_id": "hidden",
    "applicant_name": "text",
    "applicant_surname": "text",
    "email": "text",
    "telephone_number_apply_form": "text",
    "similar_employers_cv[]": "checkbox",
    "headhunters[]": "checkbox",
}


@dataclass(frozen=True)
class PortalAssessment:
    adapter: str
    form_url: str
    fingerprint: str
    field_names: tuple[str, ...]
    terms_url: str
    issues: tuple[str, ...]
    requires_javascript_upload: bool = True
    submission_enabled: bool = False

    def as_dict(self):
        return asdict(self)


def secret_url(url: str) -> str:
    url = canonical_url(url)
    parts = urlsplit(url)
    if (
        parts.hostname != SECRET_HOST
        or not re.fullmatch(r"/job/[a-z0-9-]+/?", parts.path)
        or (parts.query and parse_qs(parts.query, keep_blank_values=True) != {"form": ["apply"]})
    ):
        raise ValueError("Use a Secret Tel Aviv vacancy URL")
    return url


def inspect_secret(data: bytes, url: str) -> PortalAssessment:
    url = secret_url(url)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Portal page exceeds the inspection limit")
    soup = BeautifulSoup(data, "html.parser")
    forms = soup.select("form#wpjb-apply-form")
    if len(forms) != 1:
        return PortalAssessment(
            "secret_telaviv", url, "", (), "", ("application_form_missing_or_ambiguous",)
        )
    form = forms[0]
    issues = []
    action = canonical_url(urljoin(url, str(form.get("action") or url)))
    before, after = urlsplit(url), urlsplit(action)
    if (
        str(form.get("method", "get")).casefold() != "post"
        or after.hostname != before.hostname
        or after.path != before.path
        or parse_qs(after.query) != {"form": ["apply"]}
    ):
        issues.append("application_action_changed")
    controls = form.find_all(["input", "select", "textarea", "button"])
    fields = [
        (str(node.get("name")), str(node.get("type", node.name)).casefold())
        for node in controls
        if node.get("name")
    ]
    names = [name for name, _ in fields]
    if len(names) != len(set(names)):
        issues.append("duplicate_fields")
    if dict(fields) != EXPECTED_FIELDS:
        issues.append("application_fields_changed")
    if any(
        node.get("name") in {"similar_employers_cv[]", "headhunters[]"} and node.has_attr("checked")
        for node in controls
    ):
        issues.append("consent_preselected")
    if any(node.has_attr("disabled") or node.has_attr("formaction") for node in controls):
        issues.append("application_controls_changed")
    if form.select(".g-recaptcha, .h-captcha, [name*='captcha'], [data-sitekey]"):
        issues.append("user_challenge_required")
    if not (
        form.select_one("#wpjb-upload-container-file")
        and form.select_one("#wpjb-upload-browse-file")
    ):
        issues.append("attachment_widget_changed")
    terms = form.select_one(".wpjb-element-name-similar_employers_cv a[href]")
    terms_url = ""
    if terms:
        try:
            terms_url = canonical_url(urljoin(url, str(terms["href"])))
            if urlsplit(terms_url).hostname not in {"www.secrettelaviv.com", "secrettelaviv.com"}:
                issues.append("consent_destination_changed")
        except ValueError:
            issues.append("consent_destination_changed")
    else:
        issues.append("terms_link_missing")
    # Exclude hidden token values, cookies, applicant data and volatile upload sessions.
    job_id = form.select_one('[name="_job_id"]')
    structure = {
        "action": action,
        "job_id": str(job_id.get("value", "")) if job_id else "",
        "fields": fields,
        "terms_url": terms_url,
        "consent_labels": [
            " ".join(node.stripped_strings)
            for node in form.select(
                ".wpjb-element-name-similar_employers_cv, .wpjb-element-name-headhunters"
            )
        ],
    }
    fingerprint = hashlib.sha256(json.dumps(structure, sort_keys=True).encode()).hexdigest()
    return PortalAssessment(
        "secret_telaviv", action, fingerprint, tuple(names), terms_url, tuple(issues)
    )


def assess_secret(url: str) -> PortalAssessment:
    url = secret_url(url)
    data, effective_url = fetch_public_document(url)
    return inspect_secret(data, secret_url(effective_url))
