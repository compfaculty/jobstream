import hashlib
import uuid
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError

from jobs.models import BatchItem, Document
from jobs.services.batches import approve_items, cancel, create, prepare

pytestmark = pytest.mark.django_db


@pytest.fixture
def document(user, settings, tmp_path):
    from django.core.files.base import ContentFile

    settings.MEDIA_ROOT = tmp_path
    data = b"%PDF-1.7\nfixture original CV\n%%EOF"
    doc = Document(owner=user, name="CV", sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    doc.file.save("cv.pdf", ContentFile(data), save=False)
    doc.save()
    return doc


def test_creation_is_idempotent_and_owner_scoped(user, other, job, document):
    key = uuid.uuid4()
    batch = create(user, document.pk, [job.pk, job.pk], key)
    assert create(user, document.pk, [job.pk], key).pk == batch.pk
    assert batch.items.count() == 1
    with pytest.raises(ValidationError):
        create(other, document.pk, [job.pk], uuid.uuid4())


def test_preparation_does_not_send_or_overwrite(user, job, document, connection):
    job.apply_email = "jobs@example.com"
    job.save()
    batch = create(user, document.pk, [job.pk], uuid.uuid4())
    item = batch.items.get()
    prepare(item.pk)
    item.refresh_from_db()
    assert item.state == "ready"
    assert item.application.status == "draft"
    prepare(item.pk)
    assert user.application_set.count() == 1
    approve_items(user, batch.pk, [item.pk])
    item.application.refresh_from_db()
    assert item.application.status == "approved"


def test_manual_portal_and_cancel(user, job, document):
    batch = create(user, document.pk, [job.pk], uuid.uuid4())
    item = batch.items.get()
    prepare(item.pk)
    item.refresh_from_db()
    assert item.error_code == "manual_portal_required"
    assert item.application_id is not None
    assert item.application.recipient == ""
    cancel(user, batch.pk)
    with pytest.raises(ValidationError):
        approve_items(user, batch.pk, [item.pk])


def test_cancelled_queue_is_never_prepared(user, job, document):
    batch = create(user, document.pk, [job.pk], uuid.uuid4())
    cancel(user, batch.pk)
    item = batch.items.get()
    prepare(item.pk)
    assert BatchItem.objects.get(pk=item.pk).state == "cancelled"
    assert not user.application_set.exists()


def test_batch_review_is_private(authenticated, other, job, document, user):
    batch = create(user, document.pk, [job.pk], uuid.uuid4())
    assert authenticated.get(f"/batches/{batch.pk}/").status_code == 200
    authenticated.force_login(other)
    assert authenticated.get(f"/batches/{batch.pk}/").status_code == 404


def test_rate_limit_retry_keeps_approval(application, connection):
    from jobs.services.submissions import approve, retry_rate_limited

    approve(application.owner, application.pk)
    application.refresh_from_db()
    digest = application.approval_digest
    application.status = "submitting"
    application.send_attempts = 1
    application.save()
    retry_rate_limited(application.pk, "600")
    application.refresh_from_db()
    from django.utils import timezone

    assert application.status == "approved"
    assert application.approval_digest == digest
    assert (application.next_dispatch_at - timezone.now()).total_seconds() > 590
    application.status = "submitting"
    application.send_attempts = 5
    application.save()
    retry_rate_limited(application.pk, "600")
    application.refresh_from_db()
    assert application.status == "failed"
    assert application.error_code == "rate_limit_retries_exhausted"


def test_cancel_approved_prevents_dispatch(user, job, document, connection):
    job.apply_email = "jobs@example.com"
    job.save()
    batch = create(user, document.pk, [job.pk], uuid.uuid4())
    item = batch.items.get()
    prepare(item.pk)
    approve_items(user, batch.pk, [item.pk])
    cancel(user, batch.pk)
    item.refresh_from_db()
    assert item.state == "cancelled"
    assert item.application.status == "draft"
    assert item.application.approval_digest == ""


def test_inactive_job_blocks_approval(application, connection):
    from jobs.services.submissions import approve

    application.job.active = False
    application.job.save()
    with pytest.raises(ValidationError, match="no longer active"):
        approve(application.owner, application.pk)


def test_batch_api_validation_and_scope(authenticated, user, other, job, document):
    payload = {"document": document.pk, "jobs": [job.pk], "request_key": str(uuid.uuid4())}
    response = authenticated.post("/api/v1/batches/", payload, content_type="application/json")
    assert response.status_code == 201
    batch_id = response.json()["id"]
    assert authenticated.get(f"/api/v1/batches/{batch_id}/items/").json()[0]["state"] == "queued"
    assert (
        authenticated.post(
            f"/api/v1/batches/{batch_id}/approve/", {"items": []}, content_type="application/json"
        ).status_code
        == 400
    )
    authenticated.force_login(other)
    assert authenticated.get(f"/api/v1/batches/{batch_id}/").status_code == 404
    assert (
        authenticated.post(
            "/api/v1/batches/",
            {**payload, "request_key": str(uuid.uuid4())},
            content_type="application/json",
        ).status_code
        == 400
    )


def test_preparation_failure_retries_then_exhausts(user, job, document, monkeypatch):
    from jobs.services.resumes import confirm

    profile = confirm(user, document, "Alice", "", {"Skills": "Python"})
    batch = create(user, document.pk, [job.pk], uuid.uuid4(), profile_id=profile.pk)
    item = batch.items.get()
    monkeypatch.setattr("jobs.services.resumes.tailor", Mock(side_effect=OSError))
    from django.utils import timezone

    for _ in range(3):
        BatchItem.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())
        prepare(item.pk)
        item.refresh_from_db()
        assert item.state == "queued"
    BatchItem.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())
    prepare(item.pk)
    item.refresh_from_db()
    assert item.state == "failed"
    assert item.error_code == "preparation_retries_exhausted"


def test_portal_materials_can_be_edited_but_not_sent(user, job, document, connection):
    from jobs.services import submissions

    batch = create(user, document.pk, [job.pk], uuid.uuid4())
    item = batch.items.get()
    prepare(item.pk)
    item.refresh_from_db()
    app = item.application
    assert app.channel == "portal" and not app.recipient
    submissions.edit(
        user, app.pk, {"subject": "Prepared portal application", "body": "My factual background"}
    )
    with pytest.raises(ValidationError, match="employer portal"):
        submissions.approve(user, app.pk)
