"""Local, advisory PDF-text import. Never persist or confirm inferred fields."""

import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email

HEADINGS = {
    "summary": ("summary", "professional summary", "profile", "professional profile", "about me"),
    "skills": (
        "skills",
        "technical skills",
        "core skills",
        "core competencies",
        "areas of expertise",
    ),
    "experience": (
        "experience",
        "professional experience",
        "work experience",
        "employment history",
        "career history",
        "relevant experience",
    ),
    "education": ("education", "education and training", "academic background"),
    "projects": ("projects", "selected projects", "personal projects"),
    "languages": ("languages", "language skills"),
    "certifications": (
        "certifications",
        "certifications and licenses",
        "licenses and certifications",
    ),
    "other": ("additional information", "additional facts", "interests", "awards", "publications"),
}
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
PHONE = re.compile(r"(?<!\w)(?:\+\d[\d ().-]{7,}\d|\d[\d ().-]{8,}\d)(?!\w)")
URL = re.compile(r"(?:https?://|www\.|(?:linkedin|github)\.com/)[^\s|<>]+", re.I)


def from_text(text: str) -> dict[str, str]:
    """Recognize common headings and header contacts; preserve ambiguous prose."""
    if len(text) > 60000:
        raise ValueError("CV text exceeds the extraction limit")
    sections: dict[str, list[str]] = {key: [] for key in HEADINGS}
    header: list[str] = []
    section = None
    for line in text.splitlines():
        line = line.strip()
        heading = line.casefold().strip(" :–—-•").replace("&", "and")
        matched = next((key for key, aliases in HEADINGS.items() if heading in aliases), None)
        if matched:
            section = matched
        elif section:
            sections[section].append(line)
        else:
            header.append(line)
    result = {key: "\n".join(lines).strip() for key, lines in sections.items() if any(lines)}
    header = [line for line in header if line]
    if not header:
        return result
    # Names/headlines are suggestions, never assertions about the candidate.
    if len(header[0]) <= 150 and re.fullmatch(r"[^\d@|:/]{2,150}", header[0]):
        result["name"] = header.pop(0)
    contact_lines, headline, remainder = [], [], []
    contact_started = False
    for line in header:
        if EMAIL.search(line) or PHONE.search(line) or URL.search(line):
            contact_started = True
            contact_lines.append(line)
        elif not contact_started and not result.get("summary") and not result.get("skills"):
            remainder.append(line)
        elif not contact_started and len(" ".join([*headline, line])) <= 200:
            headline.append(line)
        else:
            remainder.append(line)
    if headline:
        result["headline"] = " ".join(headline)
    leftovers = []
    for line in contact_lines:
        for match in EMAIL.finditer(line):
            try:
                validate_email(match.group())
            except ValidationError:
                continue
            result.setdefault("email", match.group())
        for match in PHONE.finditer(line):
            phone = match.group().strip()
            if 7 <= sum(c.isdigit() for c in phone) <= 15 and len(phone) <= 50:
                result.setdefault("phone", phone)
        for match in URL.finditer(line):
            url = match.group().rstrip(".,;)")
            url = url if url.lower().startswith(("http://", "https://")) else "https://" + url
            try:
                URLValidator(schemes=["https", "http"])(url)
            except ValidationError:
                continue
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            field = (
                "linkedin"
                if host == "linkedin.com"
                else "github"
                if host == "github.com"
                else "website"
            )
            if len(url) <= 300:
                result.setdefault(field, url)
        # Contacts often share a line with location. Only accept a short remainder.
        rest = URL.sub("", EMAIL.sub("", PHONE.sub("", line)))
        rest = re.sub(
            r"\b(?:email|phone|tel|mobile|linkedin|github|website)\s*:", "", rest, flags=re.I
        )
        rest = rest.strip(" |·•,;")
        if rest:
            leftovers.append(rest)
    if len(leftovers) == 1 and len(leftovers[0]) <= 150:
        result["location"] = leftovers[0]
    else:
        remainder.extend(leftovers)
    # Keep the original contact block too: alternate contacts and unfamiliar layouts
    # must remain available even when individual fields cannot be recognized.
    if contact_lines:
        remainder.extend(contact_lines)
    if remainder:
        result["other"] = "\n\n".join(filter(None, [result.get("other", ""), "\n".join(remainder)]))
    return result
