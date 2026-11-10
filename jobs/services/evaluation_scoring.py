"""Evidence-backed requirement coverage, with server-enforced preference overrides."""

from django.core.exceptions import ValidationError

from jobs.models import ApplicationPreferences

from .agent_gateway import GatewayError
from .matching import technologies
from .normalization import normalize_description
from .preference_matching import evaluate


def criteria_for(job: dict) -> list[dict]:
    normalized = job.get("normalized")
    if not isinstance(normalized, dict) or not normalized.get("requirements"):
        normalized = normalize_description(job.get("description", ""))
    # Score stack coverage only; prose qualifications are not independent criteria.
    weights = {}
    for section in ("requirements", "preferred"):
        weight = 3 if section == "requirements" else 1
        for text in normalized.get(section, []):
            if not isinstance(text, str) or not text.strip():
                continue
            if text.strip().casefold().rstrip(":") in {
                "advantages",
                "nice to have",
                "preferred qualifications",
            }:
                weight = 1
                continue
            for skill in technologies(text):
                weights[skill] = max(weights.get(skill, 0), weight)
    if not weights:
        # Unsectioned postings still expose a stack, without inventing requirements.
        for skill in technologies(job.get("description", "")):
            weights[skill] = 3
    if not weights:
        raise ValidationError(
            "This vacancy has no recognized technologies to compare with your CV."
        )
    return [
        {"id": f"req-{index}", "text": skill, "weight": weights[skill]}
        for index, skill in enumerate(sorted(weights), start=1)
    ]


def preference_blocks(job: dict, preferences: dict) -> list[str]:
    # Exclusions are absolute, including optional mentions and language alternatives.
    mentioned = technologies(job.get("title", "") + "\n" + job.get("description", ""))
    excluded = sorted(mentioned & set(preferences.get("excluded_technologies", [])))
    blocks = [f"This job mentions {name}, which you excluded." for name in excluded]
    # Check the remaining hard preferences independently of CV technology overlap.
    policy = ApplicationPreferences(**{**preferences, "excluded_technologies": []})
    result = evaluate(job, policy)
    messages = {
        "fit_location_unknown": "The required location could not be verified.",
        "fit_location_excluded": "The job location does not match your required locations.",
        "fit_title_excluded": "The job title does not match your required titles.",
        "fit_wanted_technology_missing": "The job does not mention a technology you require.",
        "fit_remote_required": "The job does not meet your remote-only preference.",
        "fit_workplace_unknown": "Remote work could not be verified for your remote-only setting.",
    }
    if result.state != "eligible" and any(
        preferences.get(key)
        for key in ("location_required", "title_required", "technology_required", "remote_only")
    ):
        blocks.append(messages.get(result.reason, "A required preference could not be verified."))
    return blocks


def validate_assessment(snapshot: dict, result: object) -> dict:
    if not isinstance(result, dict) or set(result) != {"summary", "confidence", "assessments"}:
        raise GatewayError("agent_invalid_result")
    if (
        not isinstance(result["summary"], str)
        or not 1 <= len(result["summary"].strip()) <= 300
        or not isinstance(result["confidence"], str)
        or result["confidence"] not in {"low", "medium", "high"}
        or not isinstance(result["assessments"], list)
    ):
        raise GatewayError("agent_invalid_result")
    criteria = {item["id"]: item for item in snapshot["criteria"]}
    facts = {item["id"]: " ".join(item["text"].split()) for item in snapshot["facts"]}
    assessments, seen, earned = [], set(), 0
    points = {"met": 2, "partial": 1, "not_demonstrated": 0}
    for item in result["assessments"]:
        keys = {"requirement_id", "status", "evidence_fact_id", "evidence_quote", "explanation"}
        if (
            not isinstance(item, dict)
            or set(item) != keys
            or any(not isinstance(value, str) for value in item.values())
        ):
            raise GatewayError("agent_invalid_result")
        key, status = item["requirement_id"], item["status"]
        if (
            key not in criteria
            or key in seen
            or status not in points
            or not 1 <= len(item["explanation"].strip()) <= 240
            or len(item["evidence_quote"]) > 240
        ):
            raise GatewayError("agent_unsupported_claim")
        seen.add(key)
        quote = " ".join(item["evidence_quote"].split())
        fact_id = item["evidence_fact_id"]
        if status in {"met", "partial"}:
            if fact_id not in facts or not quote or quote not in facts[fact_id]:
                raise GatewayError("agent_unsupported_claim")
        elif fact_id or quote:
            raise GatewayError("agent_unsupported_claim")
        earned += criteria[key]["weight"] * points[status]
        assessments.append(
            {
                **item,
                "requirement": criteria[key]["text"],
                "weight": criteria[key]["weight"],
                "label": {
                    "met": "Demonstrated",
                    "partial": "Partially demonstrated",
                    "not_demonstrated": "Not demonstrated in CV",
                }[status],
            }
        )
    if seen != set(criteria):
        raise GatewayError("agent_invalid_result")
    total = sum(item["weight"] for item in criteria.values()) * 2
    score = (earned * 100 + total // 2) // total
    blocks = snapshot["preference_blocks"]
    return {
        "fit": snapshot["fit"],
        "ai_selection": result,
        "match_score": 0 if blocks else score,
        "cv_match_score": score,
        "summary": result["summary"],
        "confidence": result["confidence"],
        "preference_blocks": blocks,
        "assessments": assessments,
        "strengths": [item for item in assessments if item["status"] == "met"][:2],
        "gaps": [item for item in assessments if item["status"] != "met"][:2],
    }
