"""One base CV; one current, editable attachment per application."""

import hashlib
import uuid

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction

from jobs.models import Application, Document, GmailConnection, PreparedResume

from . import resumes, submissions
from .documents import current_base, discard_generated_document, generated_cv_filename


def editor_initial(owner, document):
    profile = document.profiles.filter(owner=owner).order_by("-pk").first()
    if not profile:
        prepared = document.prepared_resumes.filter(owner=owner).select_related("profile").first()
        if prepared and prepared.provenance.get("normalizer_version"):
            profile = prepared.profile
    if profile:
        initial = {
            section.lower(): "\n\n".join(
                fact["text"] for fact in profile.facts if fact["section"] == section
            )
            for section in resumes.SECTIONS
        }
        initial.update(name=profile.name, contact=profile.contact)
        email = profile.details.get("basics", {}).get("email", "")
    else:
        from .profile_prefill import from_text

        initial = from_text(resumes.extract(document))
        email = initial.get("email", "")
        initial["contact"] = " | ".join(
            initial.get(k, "")
            for k in ("phone", "location", "linkedin", "website", "github", "contact")
            if initial.get(k)
        )
    connection = GmailConnection.objects.filter(owner=owner, revoked=False).first()
    chosen_email = connection.email if connection else email or owner.email
    # Imported contact-only paragraphs already appear in the header, not in Other.
    initial["contact"] = " | ".join(
        part.strip()
        for part in initial.get("contact", "").split("|")
        if part.strip() and part.strip() not in {email, chosen_email}
    )
    if email and chosen_email != email:
        for key in ("summary", "skills", "experience", "education", "projects", "other"):
            initial[key] = initial.get(key, "").replace(email, chosen_email)
    initial["email"] = chosen_email
    return initial


def generate(owner, application_id):
    base = current_base(owner)
    if not base:
        raise ValidationError("Upload your base CV first.")
    profile = base.profiles.filter(owner=owner).order_by("-pk").first()
    if not profile:
        raise ValidationError("Edit and save your base CV first, then generate the application CV.")
    connection = GmailConnection.objects.filter(owner=owner, revoked=False).first()
    email = connection.email if connection else profile.details.get("basics", {}).get("email", "")
    application = Application.objects.get(pk=application_id, owner=owner)
    old_document_id = application.document_id
    result = resumes.prepare_application_cv(owner, application_id, profile.pk, email)
    if old_document_id != result.document_id:
        discard_generated_document(owner, old_document_id)
    return result


def save_editor(owner, original, values, *, application=None):
    """Never modify bytes behind a reviewed or previously sent attachment."""
    sections = {section: values.get(section.lower(), "") for section in resumes.SECTIONS}
    contact = " | ".join(filter(None, [values["email"], values.get("contact", "")]))
    # Use confirm's bounded paragraph validation before writing a file.
    with transaction.atomic():
        from django.contrib.auth import get_user_model

        get_user_model().objects.select_for_update(no_key=True).get(pk=owner.pk)
        if application:
            current = Application.objects.select_for_update().get(pk=application.pk, owner=owner)
            if current.updated_at != application.updated_at or current.status not in {
                "draft",
                "failed",
                "approved",
            }:
                raise ValidationError(
                    "Application changed or was submitted. Reload before editing."
                )
        elif current_base(owner).pk != original.pk:
            raise ValidationError("Your base CV changed. Reload before editing.")
        # All new values were entered and saved by the owner; old versions remain
        # immutable only while referenced by actual applications or provenance.
        profile = resumes.confirm(
            owner,
            original,
            values["name"],
            contact,
            sections,
            details={"basics": {"email": values["email"]}},
        )
        facts = profile.facts
        job = (
            {
                "id": application.job_id,
                "title": application.job.title,
                "company": application.job.company,
                "description": application.job.description,
            }
            if application
            else None
        )
        data = resumes.pdf_bytes(values["name"], contact, facts, job=job)
        filename = generated_cv_filename(
            values["name"],
            application.job.title if application else "Base",
            application.job.company if application else "",
        )
        document = Document(
            owner=owner, name=filename, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )
        document.file.save(filename, ContentFile(data), save=False)
        try:
            document.save()
            # The editable text belongs to this exact PDF, not another version.
            profile.document = document
            profile.save(update_fields=["document"])
            if application:
                PreparedResume.objects.create(
                    key=uuid.uuid4().hex,
                    owner=owner,
                    profile=profile,
                    document=document,
                    provenance={"job": job, "sha256": document.sha256, "owner_edited": True},
                    body=application.body,
                )
                answers = dict(current.portal_answers)
                if "email" in answers:
                    answers["email"] = values["email"]
                if "agree_terms" in answers:
                    answers["agree_terms"] = False
                edited = submissions.edit(
                    owner, current.pk, {"document": document, "portal_answers": answers}
                )
                from .secret_telaviv import supported as secret_supported

                if secret_supported(edited):
                    edited.portal_fingerprint = ""
                    edited.save(update_fields=["portal_fingerprint"])
        except Exception:
            document.file.delete(save=False)
            raise
    if application:
        discard_generated_document(owner, original.pk)
    return document
