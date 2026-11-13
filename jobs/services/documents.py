import hashlib
import re
import unicodedata

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from jobs.models import AuditEvent, Document

MAX_SIZE = 5 * 1024 * 1024


def pdf_filename(name: str) -> str:
    """Bounded ASCII filename for headers, MIME and strict employer uploaders."""
    stem = name.removesuffix(".pdf").removesuffix(".PDF")
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_")[:140].rstrip("_")
    return (stem or "CV") + ".pdf"


def generated_cv_filename(person: str, role: str, company: str) -> str:
    return pdf_filename("_".join(filter(None, [person, company, role, "CV"])))


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


def base_documents(owner):
    """Only the most recent uploaded/base-edited CV is selectable as the base."""
    candidates = Document.objects.filter(owner=owner, prepared_resumes__isnull=True)
    return candidates.filter(pk__in=candidates.order_by("-created_at", "-pk").values("pk")[:1])


def current_base(owner):
    return base_documents(owner).first()


def discard_generated_document(owner, document_id):
    """Remove a superseded private draft, never a referenced application attachment."""
    from django.db.models.deletion import ProtectedError

    from jobs.models import Application, PreparedResume

    if not document_id or Application.objects.filter(document_id=document_id).exists():
        return False
    try:
        with transaction.atomic():
            document = Document.objects.select_for_update().get(pk=document_id, owner=owner)
            if not PreparedResume.objects.filter(document=document, owner=owner).exists():
                return False  # Uploaded/base CVs are never garbage-collected here.
            PreparedResume.objects.filter(document=document, owner=owner).delete()
            document.profiles.all().delete()
            document.delete()
            transaction.on_commit(lambda: document.file.delete(save=False))
    except ProtectedError, Document.DoesNotExist:
        return False  # Still referenced by a batch, evaluation or source profile.
    return True
