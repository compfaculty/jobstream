import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ValidationError

from jobs.models import ApplicationPreferences, SavedApplicationAnswers
from jobs.services.preference_matching import evaluate
from jobs.services.saved_answers import prefill_contact, read_answers, save_answers

pytestmark = pytest.mark.django_db


def policy(**kwargs):
    return ApplicationPreferences(**kwargs)


def test_soft_preferences_do_not_exclude_but_record_matches():
    pref = policy(
        preferred_locations=["Israel"], preferred_titles=["Backend"], wanted_technologies=["Python"]
    )
    result = evaluate(
        {
            "title": "Senior Backend Engineer",
            "location": "Tel Aviv",
            "description": "Requirements\nPython",
        },
        pref,
        "Python",
    )
    assert result.state == "eligible"
    assert result.preferred_matches == ("Location: Israel", "Title: Backend", "Technology: Python")
    assert (
        evaluate(
            {"title": "DevOps", "location": "London", "description": "Requirements\nAWS"},
            pref,
            "AWS",
        ).state
        == "eligible"
    )


def test_explicit_limits_and_title_word_boundaries():
    pref = policy(preferred_locations=["Israel"], location_required=True)
    assert evaluate({"location": "Rehovot", "description": "Python"}, pref).state == "eligible"
    assert (
        evaluate({"location": "Paris, France", "description": "Python"}, pref).reason
        == "fit_location_excluded"
    )
    assert evaluate({"location": "Remote", "description": "Python"}, pref).state == "needs_action"
    pref = policy(preferred_titles=["CTO"], title_required=True)
    assert evaluate({"title": "Director", "description": "Python"}, pref).state == "skipped"
    assert evaluate({"title": "Field CTO", "description": "Python"}, pref).state == "eligible"
    pref = policy(wanted_technologies=["Python"], technology_required=True)
    assert evaluate({"description": "Requirements\nRust"}, pref, "Rust").state == "skipped"
    assert evaluate({"description": "Requirements\nPython"}, pref, "Rust").state == "skipped"


@pytest.mark.parametrize(
    "location,description,state",
    [
        ("Remote, Israel", "Python", "eligible"),
        ("Israel", "Fully remote role", "eligible"),
        ("Israel", "On-site role", "skipped"),
        ("Israel", "Hybrid role", "skipped"),
        ("Israel", "Python", "needs_action"),
        ("Israel", "This is not remote", "skipped"),
    ],
)
def test_remote_only_is_explicit(location, description, state):
    assert (
        evaluate({"location": location, "description": description}, policy(remote_only=True)).state
        == state
    )


def test_preferences_validate_conflicts_and_empty_limits(authenticated, user):
    response = authenticated.post(
        "/preferences/",
        {
            "preferred_locations": "Israel\nTel Aviv",
            "preferred_titles": "Backend\nBackend",
            "wanted_technologies": ["Python"],
            "excluded_technologies": ["Java"],
            "location_required": "on",
        },
    )
    assert response.status_code == 302
    prefs = ApplicationPreferences.objects.get(owner=user)
    assert prefs.preferred_locations == ["Israel", "Tel Aviv"] and prefs.preferred_titles == [
        "Backend"
    ]
    for data in [
        {"location_required": "on"},
        {"wanted_technologies": ["Python"], "excluded_technologies": ["Python"]},
    ]:
        assert authenticated.post("/preferences/", data).status_code == 200
    prefs.refresh_from_db()
    assert prefs.wanted_technologies == ["Python"]
    assert authenticated.post("/preferences/", {}).status_code == 302
    prefs.refresh_from_db()
    assert prefs.preferred_locations == [] and not prefs.location_required


def test_answer_storage_is_encrypted_opt_in_and_owner_bound(user, other, settings):
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    values = {
        "first_name": "Alice",
        "email": "private@example.test",
        "gender_identity": "prefer_not_to_answer",
    }
    save_answers(user.pk, values, reuse_enabled=False)
    record = SavedApplicationAnswers.objects.get(owner=user)
    assert (
        "Alice" not in record.encrypted_answers
        and "private@example.test" not in record.encrypted_answers
    )
    assert read_answers(user.pk) == (values, False) and prefill_contact(user.pk) == {}
    assert read_answers(other.pk) == ({}, False)
    save_answers(user.pk, values, reuse_enabled=True)
    assert prefill_contact(user.pk) == {"first_name": "Alice", "email": "private@example.test"}
    record.refresh_from_db()
    SavedApplicationAnswers.objects.create(
        owner=other, encrypted_answers=record.encrypted_answers, reuse_enabled=True
    )
    with pytest.raises(ValidationError):
        read_answers(other.pk)


def test_answer_edit_delete_and_separate_preference_forms(authenticated, user, other, settings):
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    ApplicationPreferences.objects.create(owner=user, excluded_technologies=["Java"])
    data = {
        "action": "save_answers",
        "answers-first_name": "Alice",
        "answers-email": "a@example.test",
        "answers-phone": "+972501234567",
        "answers-phone_country": "IL",
        "answers-city": "Tel Aviv",
        "answers-gender_identity": "prefer_not_to_answer",
        "answers-reuse_enabled": "on",
    }
    assert authenticated.post("/preferences/", data).status_code == 302
    assert ApplicationPreferences.objects.get(owner=user).excluded_technologies == ["Java"]
    assert read_answers(user.pk)[0]["gender_identity"] == "prefer_not_to_answer"
    authenticated.force_login(other)
    assert b"a@example.test" not in authenticated.get("/preferences/").content
    assert authenticated.post("/preferences/", {"action": "delete_answers"}).status_code == 302
    assert SavedApplicationAnswers.objects.filter(owner=user).exists()
    authenticated.force_login(user)
    assert authenticated.post("/preferences/", {"action": "delete_answers"}).status_code == 302
    assert not SavedApplicationAnswers.objects.filter(owner=user).exists()


def test_missing_key_does_not_store_plaintext(authenticated, user, settings):
    settings.TOKEN_ENCRYPTION_KEY = ""
    assert (
        authenticated.post(
            "/preferences/", {"action": "save_answers", "answers-first_name": "Alice"}
        ).status_code
        == 200
    )
    assert not SavedApplicationAnswers.objects.exists()


def test_secret_contact_prefill_does_not_accept_terms(authenticated, user, job, settings):
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    save_answers(
        user.pk,
        {
            "first_name": "Alice",
            "last_name": "Example",
            "email": "a@example.test",
            "phone": "+972501234567",
            "gender_identity": "female",
        },
        reuse_enabled=True,
    )
    job.apply_url = "https://jobs.secrettelaviv.com/job/test-engineer/"
    job.save()
    response = authenticated.post("/batches/new/", {"jobs": [job.pk]})
    form = response.context["applicant_form"]
    assert form["first_name"].value() == "Alice"
    assert not form["agree_terms"].value() and not form["forward_headhunters"].value()
    assert "gender_identity" not in form.fields
