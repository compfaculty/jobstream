"""Tailor presentation using confirmed claims. No job text becomes a candidate fact."""

import copy
import hashlib
import io
import json
import re
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.validators import validate_email
from django.db import transaction
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from jobs.models import AuditEvent, Document, PreparedResume, ResumeProfile

FONT_PATH = getattr(settings, "CV_FONT_PATH", "") or str(
    Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
)

CV_FONT = TTFont("JobStreamCV", FONT_PATH)
pdfmetrics.registerFont(CV_FONT)

BOLD_FONT_PATH = getattr(settings, "CV_BOLD_FONT_PATH", "") or (
    str(Path(reportlab.__file__).parent / "fonts" / "VeraBd.ttf")
    if not getattr(settings, "CV_FONT_PATH", "")
    else FONT_PATH
)
CV_BOLD_FONT = TTFont("JobStreamCVBold", BOLD_FONT_PATH)
pdfmetrics.registerFont(CV_BOLD_FONT)

VERSION = "fact-ranking-v5-professional-filenames"
SECTIONS = ("Summary", "Skills", "Experience", "Projects", "Education", "Other")


def extract(document: Document) -> str:
    with document.file.open("rb") as stream:
        data = stream.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != document.sha256:
        raise ValidationError("The base CV has changed. Upload and review it again.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or len(reader.pages) > 20:
            raise ValueError("Unsupported document")
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        raise ValidationError(
            "Unable to extract this PDF. Enter and verify your facts manually."
        ) from exc
    if not text.strip() or len(text) > 60000:
        raise ValidationError("No usable text found. Enter and verify your facts manually.")
    return text


@transaction.atomic
def confirm(
    owner, document, name: str, contact: str, sections: dict, *, details: dict | None = None
) -> ResumeProfile:
    if document.owner_id != owner.pk:
        raise ValidationError("Document does not belong to applicant.")
    facts: list[dict[str, str]] = []
    for section in SECTIONS:
        for block in re.split(r"\n\s*\n", sections.get(section, "").strip()):
            text = block.strip()
            if not text:
                continue
            if len(text) > 5000:
                raise ValidationError(
                    "Split long sections into paragraphs shorter than 5,000 characters."
                )
            facts.append({"id": f"fact-{len(facts) + 1}", "section": section, "text": text})
    if not name.strip() or not facts or len(facts) > 200:
        raise ValidationError("Provide your name and between 1 and 200 factual paragraphs.")
    profile = ResumeProfile.objects.create(
        owner=owner,
        document=document,
        name=name.strip(),
        contact=contact.strip(),
        facts=facts,
        details=details or {},
    )
    AuditEvent.objects.create(
        actor=owner,
        event="profile.confirmed",
        details={"profile_id": profile.pk, "document_id": document.pk},
    )
    return profile


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[\w+#.]{2,}", text.casefold()))


def ranked_facts(profile: ResumeProfile, job: dict) -> list[dict]:
    desired = tokens(job.get("title", "") + " " + job.get("description", ""))
    facts = profile.facts
    # Keep all confirmed facts and their section; only reorder within sections.
    return sorted(
        facts,
        key=lambda fact: (SECTIONS.index(fact["section"]), -len(tokens(fact["text"]) & desired)),
    )


def _fact_blocks(fact: dict) -> list[tuple[str, str]]:
    """Recognize role headings without changing or interpreting factual claims.

    A profile paragraph can contain several roles and wrapped bullet lines. Keep
    those roles separate so a heading cannot be stranded at the bottom of a page.
    Unstructured paragraphs remain ordinary body text.
    """
    blocks: list[tuple[str, str]] = []
    for line in fact["text"].splitlines():
        line = line.strip()
        if not line:
            continue
        if fact["section"] == "Experience" and "|" in line and not line.startswith(("•", "-")):
            blocks.append(("role", line))
        elif line.startswith(("• ", "- ")):
            blocks.append(("bullet", line[2:]))
        elif line.isupper() and len(line) > 5:
            blocks.append(("subheading", line))
        elif blocks and blocks[-1][0] in {"bullet", "role"}:
            kind, previous = blocks[-1]
            blocks[-1] = (kind, previous + " " + line)
        elif blocks and blocks[-1][0] == "body" and not re.match(r"^[^:]{1,50}: ", line):
            blocks[-1] = ("body", blocks[-1][1] + " " + line)
        else:
            blocks.append(("body", line))
    return blocks


def _experience_blocks(facts: list[dict]) -> list[tuple[str, str]]:
    """Keep dated employment chronological; relevance must not scramble careers."""
    roles: list[list[tuple[str, str]]] = []
    other: list[tuple[str, str]] = []
    current = other
    for fact in facts:
        for block in _fact_blocks(fact):
            if block[0] == "role":
                current = [block]
                roles.append(current)
            elif block[0] == "subheading":
                current = other
                current.append(block)
            else:
                current.append(block)

    def latest_year(role):
        years = re.findall(r"\b(?:19|20)\d{2}\b", role[0][1])
        if re.search(r"\b(present|current)\b", role[0][1], re.IGNORECASE):
            return 9999
        return max(map(int, years), default=0)

    roles.sort(key=latest_year, reverse=True)
    return [block for role in roles for block in role] + other


def pdf_bytes(name: str, contact: str, facts: list[dict], *, job: dict | None = None) -> bytes:
    target = " | ".join(
        filter(None, [(job or {}).get("title", ""), (job or {}).get("company", "")])
    )
    text = name + contact + target + "".join(fact["text"] for fact in facts)
    if any(ord(char) not in CV_FONT.face.charToGlyph for char in text if not char.isspace()):
        raise ValidationError(
            "The CV font does not support some characters. "
            "Configure CV_FONT_PATH with a suitable Unicode font."
        )
    bold_text = name + "".join(
        line for fact in facts for kind, line in _fact_blocks(fact) if kind == "role"
    )
    if any(
        ord(char) not in CV_BOLD_FONT.face.charToGlyph for char in bold_text if not char.isspace()
    ):
        raise ValidationError(
            "The CV bold font does not support some characters. Configure CV_BOLD_FONT_PATH."
        )
    output = io.BytesIO()
    ink = colors.HexColor("#243247")
    navy = colors.HexColor("#193b55")
    body = ParagraphStyle(
        "CVBody",
        fontName="JobStreamCV",
        fontSize=9.5,
        leading=13,
        textColor=ink,
        spaceAfter=4,
        splitLongWords=True,
    )
    title = ParagraphStyle(
        "CVName",
        parent=body,
        fontName="JobStreamCVBold",
        fontSize=23,
        leading=28,
        textColor=navy,
        spaceAfter=8,
    )
    contact_style = ParagraphStyle(
        "CVContact",
        parent=body,
        fontSize=9,
        leading=13,
        spaceAfter=4,
    )
    heading = ParagraphStyle(
        "CVSection",
        parent=body,
        fontName="JobStreamCVBold",
        fontSize=11,
        leading=16,
        textColor=navy,
        spaceBefore=10,
        spaceAfter=5,
        keepWithNext=True,
        borderWidth=0,
        borderPadding=(0, 0, 4, 0),
    )
    role = ParagraphStyle(
        "CVRole",
        parent=body,
        fontName="JobStreamCVBold",
        fontSize=10.5,
        leading=14.5,
        textColor=navy,
        spaceBefore=8,
        spaceAfter=5,
        keepWithNext=True,
    )
    date_style = ParagraphStyle(
        "CVDates",
        parent=body,
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#596575"),
        spaceAfter=6,
        keepWithNext=True,
    )
    bullet = ParagraphStyle(
        "CVBullet",
        parent=body,
        leftIndent=11,
        firstLineIndent=0,
        bulletIndent=0,
        spaceAfter=4,
    )
    story = [Paragraph(escape(name), title)]
    if target:
        story.append(Paragraph(escape(target), contact_style))
    if contact:
        story += [Paragraph(escape(contact).replace("\n", "<br/>"), contact_style)]
    story.append(Spacer(1, 3))

    def contact_part(value):
        return "".join(value.casefold().split()).removeprefix("https://").removeprefix("http://")

    displayed_contact = {contact_part(part) for part in contact.split("|") if part.strip()}
    for section in SECTIONS:
        section_facts = [fact for fact in facts if fact["section"] == section]
        if section == "Other" and displayed_contact:
            # A parsed CV may repeat the header in an Other/contact paragraph.
            # Omit only an exact set of already displayed contact components.
            section_facts = [
                fact
                for fact in section_facts
                if not (
                    "|" in fact["text"]
                    and all(
                        contact_part(part) in displayed_contact for part in fact["text"].split("|")
                    )
                )
            ]
        if not section_facts:
            continue
        story.append(Paragraph(escape(section).upper(), heading))
        blocks = (
            _experience_blocks(section_facts)
            if section == "Experience"
            else [block for fact in section_facts for block in _fact_blocks(fact)]
        )
        for kind, line in blocks:
            if kind == "role":
                parts = [part.strip() for part in line.split("|")]
                # Only separate a date field when it contains a year. Otherwise
                # preserve the whole heading; arbitrary profile text is data.
                dates = (
                    parts.pop()
                    if len(parts) >= 3 and re.search(r"\b(?:19|20)\d{2}\b", parts[-1])
                    else ""
                )
                story.append(Paragraph(escape(" | ".join(parts)), role))
                if dates:
                    story.append(Paragraph(escape(dates), date_style))
            elif kind == "subheading":
                story.append(Paragraph(escape(line), heading))
            elif kind == "bullet":
                story.append(Paragraph(escape(line), bullet, bulletText="-"))
            else:
                story.append(Paragraph(escape(line), body))

    def page_background(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, doc.pagesize[0], doc.pagesize[1], stroke=0, fill=1)
        canvas.setStrokeColor(colors.HexColor("#dbe2e8"))
        canvas.line(doc.leftMargin, 35, doc.pagesize[0] - doc.rightMargin, 35)
        canvas.setFont("JobStreamCV", 8)
        canvas.setFillColor(colors.HexColor("#596575"))
        canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 23, str(doc.page))
        canvas.restoreState()

    SimpleDocTemplate(
        output,
        pagesize=(8.27 * inch, 11.69 * inch),
        rightMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.65 * inch,
        title=f"{name} - CV",
        author=name,
    ).build(story, onFirstPage=page_background, onLaterPages=page_background)
    data = output.getvalue()
    if len(data) > 5 * 1024 * 1024:
        raise ValidationError("Generated CV exceeds the attachment size limit.")
    return data


def tailor(
    profile: ResumeProfile, job: dict, use_ai=False, *, contact_email: str = ""
) -> tuple[Document, dict, str]:
    from .ai import VERSION as AI_VERSION

    # Overrides are presentation-only. Immutable confirmed profile versions and
    # source PDFs are never rewritten when a user chooses a contact address.
    if contact_email:
        validate_email(contact_email)
        profile = copy.copy(profile)
        profile.facts = copy.deepcopy(profile.facts)
        source_email = profile.details.get("basics", {}).get("email", "")
        if not source_email:
            matches = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", profile.contact)
            source_email = matches[0] if len(matches) == 1 else ""
        if source_email:
            profile.contact = profile.contact.replace(source_email, contact_email)
            for fact in profile.facts:
                fact["text"] = fact["text"].replace(source_email, contact_email)
        if contact_email not in profile.contact:
            profile.contact = " | ".join(filter(None, [profile.contact, contact_email]))

    key = hashlib.sha256(
        json.dumps(
            {
                "owner": profile.owner_id,
                "profile": profile.pk,
                "facts": profile.facts,
                "name": profile.name,
                "contact": profile.contact,
                "job": job,
                "version": VERSION,
                "ai": use_ai,
                "ai_version": AI_VERSION,
                "model": settings.OPENAI_MODEL if use_ai else "",
                "font": hashlib.sha256(Path(FONT_PATH).read_bytes()).hexdigest(),
                "bold_font": hashlib.sha256(Path(BOLD_FONT_PATH).read_bytes()).hexdigest(),
                "contact_email": contact_email,
                "reportlab": reportlab.Version,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    cached = (
        PreparedResume.objects.select_related("document")
        .filter(key=key, owner_id=profile.owner_id)
        .first()
    )
    if cached:
        return cached.document, cached.provenance, cached.body
    facts = ranked_facts(profile, job)
    selection = None
    if use_ai:
        from .ai import select_facts

        selection = select_facts(profile, job)
        order = {value: index for index, value in enumerate(selection["ordered_fact_ids"])}
        facts = sorted(
            profile.facts, key=lambda fact: (SECTIONS.index(fact["section"]), order[fact["id"]])
        )
    desired = tokens(job.get("title", "") + " " + job.get("description", ""))
    relevant = sorted(facts, key=lambda fact: -len(tokens(fact["text"]) & desired))[:2]
    if selection:
        by_id = {fact["id"]: fact for fact in facts}
        relevant = [by_id[value] for value in selection["highlight_fact_ids"]]
    highlights = "\n\n".join(fact["text"] for fact in relevant)
    body = (
        f"Hello,\n\nI would like to apply for {job['title']} "
        f"at {job.get('company') or 'your company'}. "
        f"Relevant details from my background:\n\n{highlights}\n\n"
        f"My CV is attached. Thank you for considering my application.\n\n{profile.name}"
    )
    if len(body) > 20000:
        raise ValidationError("Prepared email exceeds the message length limit.")
    data = pdf_bytes(profile.name, profile.contact, facts, job=job)
    from .documents import generated_cv_filename

    filename = generated_cv_filename(profile.name.title(), job["title"], job.get("company", ""))
    document = Document(
        owner_id=profile.owner_id,
        name=filename,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )
    document.file.save(filename, ContentFile(data), save=False)
    try:
        document.save()
        provenance = {
            "profile_id": profile.pk,
            "base_document_id": profile.document_id,
            "normalizer_version": VERSION,
            "contact_email": contact_email,
            "job": job,
            "fact_ids": [fact["id"] for fact in facts],
            "sha256": document.sha256,
            "ai_selection": selection,
            "preparation_key": key,
        }
        artifact, created = PreparedResume.objects.get_or_create(
            key=key,
            defaults={
                "owner_id": profile.owner_id,
                "profile": profile,
                "document": document,
                "provenance": provenance,
                "body": body,
            },
        )
    except Exception:
        document.file.delete(save=False)
        if document.pk:
            document.delete()
        raise
    if not created:
        document.file.delete(save=False)
        document.delete()
    return artifact.document, artifact.provenance, artifact.body


def prepare_application_cv(owner, application_id: int, profile_id: int, contact_email: str):
    """Generate privately, then attach only if the reviewed draft has not changed."""
    from . import submissions

    validate_email(contact_email)
    application = owner.application_set.select_related("job").get(pk=application_id)
    if application.status not in {"draft", "failed", "approved"}:
        raise ValidationError("This application's CV can no longer be changed.")
    profile = ResumeProfile.objects.get(pk=profile_id, owner=owner)
    job = {
        "id": application.job_id,
        "title": application.job.title,
        "company": application.job.company,
        "description": application.job.description,
        "location": application.job.location,
    }
    document, _, body = tailor(profile, job, contact_email=contact_email)
    with transaction.atomic():
        current = owner.application_set.select_for_update().get(pk=application_id)
        if current.updated_at != application.updated_at:
            raise ValidationError("Application changed during generation. Review it and try again.")
        answers = dict(current.portal_answers)
        if "email" in answers:
            answers["email"] = contact_email
        # Changing applicant details requires a fresh portal consent review.
        if "agree_terms" in answers:
            answers["agree_terms"] = False
        result = submissions.edit(
            owner,
            current.pk,
            {
                "document": document,
                "body": body if current.channel == "email" else current.body,
                "portal_answers": answers,
            },
        )

        from .secret_telaviv import supported as secret_supported

        if secret_supported(result):
            result.portal_fingerprint = ""
            result.save(update_fields=["portal_fingerprint"])
        return result
