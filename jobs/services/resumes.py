"""Tailor presentation using confirmed claims. No job text becomes a candidate fact."""

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
from django.db import transaction
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
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

VERSION = "fact-ranking-v1"
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


def pdf_bytes(name: str, contact: str, facts: list[dict]) -> bytes:
    text = name + contact + "".join(fact["text"] for fact in facts)
    if any(ord(char) not in CV_FONT.face.charToGlyph for char in text if not char.isspace()):
        raise ValidationError(
            "The CV font does not support some characters. "
            "Configure CV_FONT_PATH with a suitable Unicode font."
        )
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    styles["Title"].fontName = "JobStreamCV"
    body = ParagraphStyle(
        "CVBody",
        parent=styles["BodyText"],
        fontName="JobStreamCV",
        fontSize=10.5,
        leading=15,
        spaceAfter=9,
        splitLongWords=True,
    )
    heading = ParagraphStyle(
        "CVSection",
        parent=styles["Heading2"],
        fontName="JobStreamCV",
        fontSize=12,
        textColor=colors.HexColor("#193b55"),
        spaceBefore=14,
    )
    story = [Paragraph(escape(name), styles["Title"])]
    if contact:
        story += [Paragraph(escape(contact), body), Spacer(1, 8)]
    section = None
    for fact in facts:
        if fact["section"] != section:
            section = fact["section"]
            story.append(Paragraph(escape(section), heading))
        story.append(Paragraph(escape(fact["text"]).replace("\n", "<br/>"), body))

    def page_background(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, doc.pagesize[0], doc.pagesize[1], stroke=0, fill=1)
        canvas.restoreState()

    SimpleDocTemplate(
        output,
        pagesize=(8.27 * inch, 11.69 * inch),
        rightMargin=0.7 * inch,
        leftMargin=0.7 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title=f"{name} - CV",
        author=name,
    ).build(story, onFirstPage=page_background, onLaterPages=page_background)
    data = output.getvalue()
    if len(data) > 5 * 1024 * 1024:
        raise ValidationError("Generated CV exceeds the attachment size limit.")
    return data


def tailor(profile: ResumeProfile, job: dict, use_ai=False) -> tuple[Document, dict, str]:
    from .ai import VERSION as AI_VERSION

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
    data = pdf_bytes(profile.name, profile.contact, facts)
    document = Document(
        owner_id=profile.owner_id,
        name=f"CV - {job['title']}"[:150],
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )
    document.file.save("resume.pdf", ContentFile(data), save=False)
    try:
        document.save()
        provenance = {
            "profile_id": profile.pk,
            "base_document_id": profile.document_id,
            "normalizer_version": VERSION,
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
