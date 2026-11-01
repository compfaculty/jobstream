"""User-selected hard limits and soft preference evidence, separate from CV facts."""

import re
from dataclasses import replace

from jobs.models import ApplicationPreferences

from .matching import FitResult, assess, technologies

ISRAEL_LOCATIONS = (
    "Israel",
    "Tel Aviv",
    "Haifa",
    "Jerusalem",
    "Holon",
    "Rehovot",
    "Herzliya",
    "Herzlia",
    "Petah Tikva",
    "Petach Tikva",
    "Ramat Gan",
    "Raanana",
    "Ra'anana",
    "Netanya",
    "Yokneam",
    "Beer Sheva",
    "Be'er Sheva",
)
COUNTRY_ALIASES = {
    "israel": ISRAEL_LOCATIONS,
    "usa": ("US", "USA", "United States", "United States of America"),
    "us": ("US", "USA", "United States", "United States of America"),
    "united states": ("US", "USA", "United States", "United States of America"),
    "uk": ("UK", "United Kingdom", "Great Britain"),
    "united kingdom": ("UK", "United Kingdom", "Great Britain"),
}


def phrase_in(phrase: str, text: str) -> bool:
    def fold(value):
        return " ".join(value.casefold().replace("’", "'").replace("-", " ").split())

    return bool(re.search(r"(?<!\w)" + re.escape(fold(phrase)) + r"(?!\w)", fold(text)))


def preferences_for(owner_id: int) -> ApplicationPreferences:
    return ApplicationPreferences.objects.filter(
        owner_id=owner_id
    ).first() or ApplicationPreferences(owner_id=owner_id)


def evaluate(
    job: dict, preferences: ApplicationPreferences, cv_text: str | None = None
) -> FitResult:
    base = assess(job, preferences.excluded_technologies, cv_text)
    if base.state != "eligible":
        return base
    matches: list[str] = []
    location = job.get("location") or ""
    title = job.get("title") or ""
    job_stack = technologies(job.get("description") or "")
    matched_locations = [
        value
        for value in preferences.preferred_locations
        if any(
            phrase_in(alias, location) for alias in COUNTRY_ALIASES.get(value.casefold(), (value,))
        )
    ]
    matched_titles = [value for value in preferences.preferred_titles if phrase_in(value, title)]
    matched_tech = sorted(set(preferences.wanted_technologies) & job_stack)
    matches.extend(f"Location: {value}" for value in matched_locations)
    matches.extend(f"Title: {value}" for value in matched_titles)
    matches.extend(f"Technology: {value}" for value in matched_tech)
    if preferences.location_required and not matched_locations:
        unknown = not location.strip() or any(
            phrase_in(word, location)
            for word in ("remote", "worldwide", "anywhere", "global", "multiple locations")
        )
        return FitResult(
            "needs_action" if unknown else "skipped",
            "fit_location_unknown" if unknown else "fit_location_excluded",
        )
    if preferences.title_required and not matched_titles:
        return FitResult("needs_action" if not title.strip() else "skipped", "fit_title_excluded")
    if preferences.technology_required and not matched_tech:
        return FitResult(
            "needs_action" if not job.get("description") else "skipped",
            "fit_wanted_technology_missing",
        )
    if preferences.remote_only:
        description = job.get("description") or ""
        remote = phrase_in("remote", location) or bool(
            re.search(
                r"\b(?:fully remote|100% remote|remote[- ]first|work remotely)\b", description, re.I
            )
        )
        negative = bool(
            re.search(
                r"\b(?:not|no)\s+remote\b|\bremote\s+(?:work\s+)?(?:is\s+)?not",
                location + " " + description,
                re.I,
            )
        )
        if negative:
            return FitResult("skipped", "fit_remote_required")
        if not remote:
            known = bool(
                re.search(
                    r"\b(?:on[- ]site|in[- ]office|hybrid)\b", location + " " + description, re.I
                )
            )
            return FitResult(
                "skipped" if known else "needs_action",
                "fit_remote_required" if known else "fit_workplace_unknown",
            )
        matches.append("Workplace: remote")
    return replace(base, preferred_matches=tuple(matches))
