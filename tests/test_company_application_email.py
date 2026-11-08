import pytest
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.test import RequestFactory

from jobs.admin import CompanyAdmin
from jobs.models import Application, Company

pytestmark = pytest.mark.django_db


def test_priority_and_fallback(job):
    assert job.preferred_application_email == ""
    job.apply_email = "posting@example.com"
    assert job.preferred_application_email == "posting@example.com"
    company = Company.objects.create(name="Employer", application_email="hr@example.com")
    job.company_profile = company
    assert job.preferred_application_email == "hr@example.com"
    company.application_email = ""
    assert job.preferred_application_email == "posting@example.com"


@pytest.mark.parametrize("email", ["invalid", "hr@example.com\r\nBcc:other@example.com"])
def test_admin_rejects_invalid_email(user, email):
    request = RequestFactory().get("/admin/jobs/company/add/")
    request.user = user
    form_class = CompanyAdmin(Company, AdminSite()).get_form(request)
    form = form_class(data={"name": "Employer", "application_email": email})
    assert not form.is_valid()
    assert "application_email" in form.errors
    with pytest.raises(ValidationError):
        Company(name="Employer", application_email=email).full_clean()


def test_hr_email_prefills_draft_and_preserves_existing(authenticated, user, job):
    company = Company.objects.create(name="Employer", application_email="hr@example.com")
    job.company_profile = company
    job.save()
    response = authenticated.get(f"/jobs/{job.pk}/apply/")
    assert response.status_code == 200
    assert response.context["form"].initial["recipient"] == "hr@example.com"
    assert response.context["form"].instance.channel == "email"
    assert not Application.objects.exists()
    app = Application.objects.create(owner=user, job=job, recipient="saved@example.com")
    assert authenticated.get(f"/jobs/{job.pk}/apply/").status_code == 302
    app.refresh_from_db()
    assert app.recipient == "saved@example.com"
    assert app.status == "draft"


def test_nonstaff_cannot_edit_company(authenticated, job):
    company = Company.objects.create(name="Employer", application_email="hr@example.com")
    response = authenticated.post(
        f"/admin/jobs/company/{company.pk}/change/",
        {"application_email": "attacker@example.com"},
    )
    assert response.status_code == 302
    company.refresh_from_db()
    assert company.application_email == "hr@example.com"
