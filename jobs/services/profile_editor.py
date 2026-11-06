"""Structured profile input, with a lossless bridge to confirmed resume facts."""

import re
from datetime import date

from django.forms import formset_factory

from jobs.forms import ProfileEducationForm, ProfileExperienceForm, ProfileProjectForm

ENTRY_TYPES = (
    ("experience", "Experience", ProfileExperienceForm),
    ("education", "Education", ProfileEducationForm),
    ("projects", "Projects", ProfileProjectForm),
)
CONTACT_FIELDS = ("email", "phone", "location", "linkedin", "website", "github", "contact")


def entry_sets(data=None, details=None):
    details = details or {}
    result = []
    for key, label, entry_form in ENTRY_TYPES:
        factory = formset_factory(
            entry_form, extra=1, can_delete=True, max_num=30, validate_max=True, absolute_max=60
        )
        result.append(
            {
                "key": key,
                "label": label,
                "forms": factory(data, prefix=key, initial=details.get(key, [])),
            }
        )
    return result


def serialize_entries(groups):
    result = {}
    for group in groups:
        result[group["key"]] = [
            {
                key: value.strftime("%Y-%m") if isinstance(value, date) else value
                for key, value in form.cleaned_data.items()
                if key != "DELETE"
            }
            for form in group["forms"]
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]
    return result


def confirmed_sections(values, entries):
    sections = {
        name.title(): values[name]
        for name in ("summary", "skills", "experience", "projects", "education", "other")
    }
    sections["Summary"] = "\n\n".join(filter(None, [values["headline"], values["summary"]]))
    for key, label, _ in ENTRY_TYPES:
        paragraphs = []
        for entry in entries[key]:
            lines = []
            for field, value in entry.items():
                if value:
                    if field == "current":
                        lines.append("Status: Present")
                    else:
                        # A role and its supporting claims must remain one factual block.
                        value = re.sub(r"\n\s*\n", "\n", str(value))
                        lines.append(f"{field.replace('_', ' ').title()}: {value}")
            paragraphs.append("\n".join(lines))
        sections[label] = "\n\n".join(filter(None, [sections[label], *paragraphs]))
    sections["Other"] = "\n\n".join(
        filter(
            None,
            [
                sections["Other"],
                f"Languages: {values['languages']}" if values["languages"] else "",
                f"Certifications: {values['certifications']}" if values["certifications"] else "",
            ],
        )
    )
    return sections
