import hashlib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from jobs.models import AuditEvent, Document

MAX_SIZE = 5 * 1024 * 1024


def upload_document(owner, upload) -> Document:
    if upload.size > MAX_SIZE or upload.size == 0:
        raise ValidationError("Upload a PDF smaller than 5 MiB.")
    content = upload.read(MAX_SIZE + 1)
    if (
        not content.startswith(b"%PDF-")
        or b"%%EOF" not in content[-2048:]
        or len(content) > MAX_SIZE
    ):
        raise ValidationError("A valid PDF file is required.")
    # Files are never rendered inline or served by the web server.
    upload.seek(0)
    document = Document(
        owner=owner,
        name=upload.name[:150],
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )
    document.file.save("resume.pdf", upload, save=False)
    try:
        with transaction.atomic():
            document.save()
            if settings.CV_REPRESENTATIONS_ENABLED:
                from .cv_profiles import ensure

                ensure(document)
            AuditEvent.objects.create(
                actor=owner, event="document.uploaded", details={"document_id": document.pk}
            )
    except Exception:
        document.file.delete(save=False)
        raise
    return document
