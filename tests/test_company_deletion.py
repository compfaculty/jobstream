import uuid

import pytest
from django.contrib.auth.models import Permission
from django_otp import DEVICE_ID_SESSION_KEY

from jobs.models import (
    Application,
    ApplicationBatch,
    AuditEvent,
    BatchItem,
    Company,
    CompanyAlias,
    CompanyProvenance,
    Document,
    EmployerDiscoveryCampaign,
    EmployerDiscoveryLead,
    EmployerDiscoveryRun,
    IngestionRun,
    Job,
    JobOrigin,
    Source,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client, user):
    user.is_staff = user.is_superuser = True
    user.save()
    device = user.totpdevice_set.create(name="company-deletion-test", confirmed=True)
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return client


@pytest.mark.parametrize("bulk", [False, True])
def test_admin_company_delete_cascades_with_confirmation(admin_client, user, other, bulk):
    company = Company.objects.create(name="Delete me")
    unrelated = Company.objects.create(name="Keep me")
    alias = CompanyAlias.objects.create(company=company, name="Old name")
    source = Source.objects.create(
        kind=Source.Kind.COMPANY,
        adapter=Source.Adapter.RSS,
        company=company,
        name="Career page",
        url="https://example.com",
    )
    run = IngestionRun.objects.create(source=source)
    job = Job.objects.create(
        company_profile=company, title="Engineer", canonical_url="https://example.com/jobs/1"
    )
    origin = JobOrigin.objects.create(job=job, source=source)
    kept_job = Job.objects.create(
        company_profile=unrelated, title="Keep", canonical_url="https://example.org/jobs/1"
    )
    shared_source = Source.objects.create(
        adapter=Source.Adapter.RSS, name="Shared board", url="https://example.org"
    )
    JobOrigin.objects.create(job=kept_job, source=shared_source)
    document = Document.objects.create(
        owner=user, name="CV", file="unused.pdf", sha256="a" * 64, size=1
    )
    batch = ApplicationBatch.objects.create(owner=user, document=document, request_key=uuid.uuid4())
    campaign = EmployerDiscoveryCampaign.objects.create(name="Shared campaign")
    discovery_run = EmployerDiscoveryRun.objects.create(campaign=campaign)
    lead = EmployerDiscoveryLead.objects.create(
        campaign=campaign, run=discovery_run, company=company, key="a" * 64
    )
    provenance = CompanyProvenance.objects.create(
        company=company,
        campaign=campaign,
        source_lead=lead,
        provider="brave",
        external_identity="test-company",
    )
    deleted = [company, alias, source, run, job, origin, lead, provenance]
    # Company deletion is an administrative operation across all affected users.
    for owner in (user, other):
        application = Application.objects.create(owner=owner, job=job, document=document)
        if owner == user:
            item = BatchItem.objects.create(batch=batch, job=job, application=application)
            deleted.append(item)
        audit = AuditEvent.objects.create(
            actor=owner, application=application, event="draft.created"
        )
        deleted.extend([application, audit])
    kept_application = Application.objects.create(owner=user, job=kept_job)
    kept_item = BatchItem.objects.create(batch=batch, job=kept_job, application=kept_application)

    url = "/admin/jobs/company/" if bulk else f"/admin/jobs/company/{company.pk}/delete/"
    data = {"action": "delete_selected", "_selected_action": [str(company.pk)]} if bulk else {}
    preview = admin_client.post(url, data) if bulk else admin_client.get(url)
    assert preview.status_code == 200
    assert not preview.context["protected"]
    assert Company.objects.filter(pk=company.pk).exists()
    assert Job.objects.filter(pk=job.pk).exists()

    response = admin_client.post(url, {**data, "post": "yes"})
    assert response.status_code == 302
    for obj in deleted:
        assert not type(obj).objects.filter(pk=obj.pk).exists()
    for obj in (
        unrelated,
        kept_job,
        shared_source,
        document,
        batch,
        kept_application,
        kept_item,
        campaign,
        discovery_run,
    ):
        assert type(obj).objects.filter(pk=obj.pk).exists()
    assert user.__class__.objects.filter(pk__in=[user.pk, other.pk]).count() == 2


def test_company_delete_requires_related_delete_permissions(admin_client, user):
    company = Company.objects.create(name="Restricted")
    source = Source.objects.create(
        kind=Source.Kind.COMPANY,
        adapter=Source.Adapter.RSS,
        company=company,
        name="Career",
        url="https://example.com",
    )
    user.is_superuser = False
    user.save()
    user.user_permissions.add(Permission.objects.get(codename="delete_company"))
    response = admin_client.post(f"/admin/jobs/company/{company.pk}/delete/", {"post": "yes"})
    assert response.status_code == 403
    assert Company.objects.filter(pk=company.pk).exists()
    assert Source.objects.filter(pk=source.pk).exists()


def test_non_staff_cannot_delete_company(authenticated):
    company = Company.objects.create(name="Protected")
    response = authenticated.post(f"/admin/jobs/company/{company.pk}/delete/", {"post": "yes"})
    assert response.status_code == 302
    assert Company.objects.filter(pk=company.pk).exists()


def test_company_delete_requires_readonly_dependency_permission(admin_client, user):
    company = Company.objects.create(name="Restricted history")
    source = Source.objects.create(
        kind=Source.Kind.COMPANY,
        adapter=Source.Adapter.RSS,
        company=company,
        name="Career",
        url="https://example.com",
    )
    run = IngestionRun.objects.create(source=source)
    user.is_superuser = False
    user.save()
    user.user_permissions.set(
        Permission.objects.filter(content_type__app_label="jobs").exclude(
            codename="delete_ingestionrun"
        )
    )
    response = admin_client.post(f"/admin/jobs/company/{company.pk}/delete/", {"post": "yes"})
    assert response.status_code == 403
    assert Company.objects.filter(pk=company.pk).exists()
    assert IngestionRun.objects.filter(pk=run.pk).exists()
