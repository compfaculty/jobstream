import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from django.db import close_old_connections

from jobs.models import Application
from jobs.services import gmail, submissions


@pytest.mark.django_db(transaction=True)
def test_two_workers_send_once(user, application, connection, monkeypatch):
    submissions.approve(user, application.pk)
    barrier = threading.Barrier(2)
    send = Mock(return_value="one-message")
    monkeypatch.setattr(gmail, "send", send)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            submissions.submit(application.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)
    assert send.call_count == 1
    assert Application.objects.get(pk=application.pk).status == "confirmed"


@pytest.mark.django_db(transaction=True)
def test_two_workers_prepare_one_batch_item(user, job, tmp_path, settings, monkeypatch):
    import hashlib
    import uuid

    from django.core.files.base import ContentFile

    from jobs.models import Document
    from jobs.services.batches import create, prepare

    settings.MEDIA_ROOT = tmp_path
    data = b"%PDF-1.7\nfixture\n%%EOF"
    document = Document(
        owner=user, name="CV", sha256=hashlib.sha256(data).hexdigest(), size=len(data)
    )
    document.file.save("base.pdf", ContentFile(data), save=False)
    document.save()
    job.apply_email = "jobs@example.com"
    job.save()
    batch = create(user, document.pk, [job.pk], uuid.uuid4())
    item = batch.items.get()
    barrier = threading.Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            prepare(item.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)
    item.refresh_from_db()
    assert item.state == "ready"
    assert item.attempts == 1
    assert Application.objects.filter(owner=user, job=job).count() == 1
