"""Explainable CV overlap and explicit user exclusions; never infer candidate facts."""

import re
from dataclasses import asdict, dataclass

from jobs.models import ApplicationPreferences

from .normalization import TECHNOLOGIES, normalize_description


@dataclass(frozen=True)
class FitResult:
    state: str
    reason: str
    matched: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    cv_technologies: tuple[str, ...] = ()
    preferred_matches: tuple[str, ...] = ()

    def as_dict(self):
        return asdict(self)


def exclusions_for(owner_id: int) -> list[str]:
    return (
        ApplicationPreferences.objects.filter(owner_id=owner_id)
        .values_list("excluded_technologies", flat=True)
        .first()
        or []
    )


def technologies(text: str) -> set[str]:
    return set(normalize_description(text)["technologies"])


def requirement_groups(line: str) -> tuple[list[set[str]], set[str]]:
    """Recognize explicit language alternatives, including Oxford-comma lists.

    Complex prose is deliberately ambiguous rather than declaring a forbidden skill mandatory.
    """
    groups: list[set[str]] = []
    ambiguous: set[str] = set()
    line = re.sub(r"\band/or\b", "or", line, flags=re.I)
    for segment in re.split(r"\b(?:and|plus|as well as)\b|[;&]", line, flags=re.I):
        occurrences = sorted(
            (match.start(), match.end(), name)
            for name, pattern in TECHNOLOGIES.items()
            for match in re.finditer(pattern, segment, flags=0 if name == "Go" else re.I)
        )
        names = {name for _, _, name in occurrences}
        if not names:
            continue
        if re.search(r"\b(?:optional|preferred|bonus|nice to have|not required)\b", segment, re.I):
            if len(names) > 1:
                ambiguous.update(names)
            continue
        if re.search(r"\b(?:not|without|rather than|instead of)\b", segment, re.I):
            ambiguous.update(names)
            continue
        if not re.search(r"\bor\b|/", segment, re.I):
            groups.extend({name} for name in names)
            continue
        gaps = [
            segment[left[1] : right[0]].strip()
            for left, right in zip(occurrences, occurrences[1:], strict=False)
        ]
        # A, B, or C / A or B / A/B. A or B, C is ambiguous.
        if (
            len(names) > 1
            and gaps
            and all(re.fullmatch(r",|,?\s*or|/", gap, re.I) for gap in gaps)
            and re.search(r"\bor\b|/", gaps[-1], re.I)
        ):
            groups.append(names)
        else:
            ambiguous.update(names)
    return groups, ambiguous


def assess(job: dict, excluded: list[str], cv_text: str | None = None) -> FitResult:
    excluded_set = set(excluded)
    description = job.get("description") or ""
    normalized = job.get("normalized")
    if not isinstance(normalized, dict) or "requirements" not in normalized:
        normalized = normalize_description(description)
    requirements = normalized.get("requirements", [])
    if not isinstance(requirements, list) or any(
        not isinstance(line, str) for line in requirements
    ):
        return FitResult("needs_action", "fit_requirements_unknown")
    uncertain = set()
    for line in requirements:
        groups, ambiguous = requirement_groups(line)
        for group in groups:
            if group <= excluded_set:
                return FitResult(
                    "skipped", "fit_excluded_technology", excluded=tuple(sorted(group))
                )
        uncertain.update(ambiguous & excluded_set)
    if uncertain:
        return FitResult(
            "needs_action", "fit_ambiguous_requirement", excluded=tuple(sorted(uncertain))
        )
    # Exclusions mentioned in unclassified prose cannot safely be declared optional or required.
    job_stack = technologies(description)
    preferred = normalized.get("preferred", [])
    optional_stack = (
        technologies("\n".join(preferred))
        if (isinstance(preferred, list) and all(isinstance(line, str) for line in preferred))
        else set()
    )
    unclassified_exclusions = (job_stack & excluded_set) - optional_stack
    if not requirements and unclassified_exclusions:
        return FitResult(
            "needs_action",
            "fit_requirements_unknown",
            excluded=tuple(sorted(unclassified_exclusions)),
        )
    if cv_text is None:
        return FitResult("eligible", "fit_exclusions_checked")
    cv_stack = technologies(cv_text) - excluded_set
    required_stack = (
        set().union(*(technologies(line) for line in requirements)) if requirements else set()
    )
    matched = sorted(cv_stack & (required_stack or job_stack))
    if matched:
        return FitResult(
            "eligible",
            "fit_technology_overlap",
            tuple(matched),
            cv_technologies=tuple(sorted(cv_stack)),
        )
    if not description.strip() or not cv_text.strip():
        return FitResult("needs_action", "fit_requirements_unknown")
    return FitResult(
        "skipped", "fit_no_technology_overlap", cv_technologies=tuple(sorted(cv_stack))
    )
