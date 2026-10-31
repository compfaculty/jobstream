import pytest

from jobs.models import Source
from jobs.services.contacts import candidates, observe


def test_application_instructions_and_generic_hr():
    values = candidates("Send your CV to hiring@example.com\nContact HR at hr@example.com")
    assert values[0]["email"] == "hiring@example.com"
    assert values[0]["purpose"] == "application"
    assert values[1]["purpose"] == "recruiting"
    assert not candidates("For privacy concerns email privacy@example.com")
    assert not candidates("Support support@example.com")
    assert (
        candidates("Apply only through the portal. Send CV to hr@example.com")[0]["purpose"]
        == "recruiting"
    )


@pytest.mark.django_db
def test_observation_provenance_and_removal(job):
    source = Source.objects.create(
        name="Employer", adapter="jsonld", url="https://example.com/careers"
    )
    observe(job, source, job.canonical_url, "Send your CV to hiring@example.com")
    job.refresh_from_db()
    assert job.apply_email == "hiring@example.com"
    assert job.recruiting_contacts.get().source_url == job.canonical_url
    observe(job, source, job.canonical_url, "Use the application portal")
    job.refresh_from_db()
    assert not job.apply_email
    assert not job.recruiting_contacts.exists()


@pytest.mark.django_db
def test_operator_address_not_overwritten(job):
    source = Source.objects.create(
        name="Employer", adapter="jsonld", url="https://example.com/careers"
    )
    job.apply_email = "manual@example.com"
    job.save()
    observe(job, source, job.canonical_url, "Send your CV to hiring@example.com")
    job.refresh_from_db()
    assert job.apply_email == "manual@example.com"
