import copy

import pytest

from jobs.services.agent_gateway import GatewayError
from jobs.services.evaluation_scoring import criteria_for, preference_blocks, validate_assessment


def snapshot():
    return {
        "criteria": [
            {"id": "req-1", "text": "Go or C++", "weight": 3},
            {"id": "req-2", "text": "GPU serving", "weight": 1},
        ],
        "facts": [{"id": "f1", "text": "Built Go\n \nservices and Docker tools."}],
        "preference_blocks": [],
        "fit": {"state": "eligible"},
    }


def assessment():
    return {
        "summary": "Strong backend evidence; GPU serving is not demonstrated.",
        "confidence": "medium",
        "assessments": [
            {
                "requirement_id": "req-1",
                "status": "met",
                "evidence_fact_id": "f1",
                "evidence_quote": "Go services",
                "explanation": "Go experience is documented.",
            },
            {
                "requirement_id": "req-2",
                "status": "not_demonstrated",
                "evidence_fact_id": "",
                "evidence_quote": "",
                "explanation": "No production GPU evidence in CV.",
            },
        ],
    }


def test_weighted_percentage_and_pdf_whitespace():
    result = validate_assessment(snapshot(), assessment())
    assert result["match_score"] == result["cv_match_score"] == 75
    value = assessment()
    value["assessments"][0]["status"] = "partial"
    assert validate_assessment(snapshot(), value)["match_score"] == 38
    for item in value["assessments"]:
        item.update(status="not_demonstrated", evidence_fact_id="", evidence_quote="")
    assert validate_assessment(snapshot(), value)["match_score"] == 0


@pytest.mark.parametrize("excluded", ["C++", "Java"])
def test_excluded_language_alternative_forces_zero(excluded):
    source = snapshot()
    job = {"title": "Platform Lead", "description": f"Go or {excluded} or Rust"}
    source["preference_blocks"] = preference_blocks(job, {"excluded_technologies": [excluded]})
    result = validate_assessment(source, assessment())
    assert result["match_score"] == 0
    assert result["cv_match_score"] == 75
    assert excluded in result["preference_blocks"][0]


def test_java_exclusion_does_not_match_javascript():
    assert (
        preference_blocks(
            {"description": "JavaScript development"}, {"excluded_technologies": ["Java"]}
        )
        == []
    )


@pytest.mark.parametrize(
    "policy,job",
    [
        (
            {"preferred_locations": ["Israel"], "location_required": True},
            {"location": "Paris", "description": "Python"},
        ),
        (
            {"preferred_titles": ["Backend"], "title_required": True},
            {"title": "Designer", "description": "Python"},
        ),
        ({"wanted_technologies": ["Rust"], "technology_required": True}, {"description": "Python"}),
        ({"remote_only": True}, {"location": "Tel Aviv", "description": "On-site Python"}),
    ],
)
def test_other_hard_preferences_force_zero(policy, job):
    source = snapshot()
    source["preference_blocks"] = preference_blocks(job, policy)
    assert source["preference_blocks"]
    assert validate_assessment(source, assessment())["match_score"] == 0


def test_soft_preferences_do_not_force_zero():
    assert (
        preference_blocks(
            {"title": "Designer", "location": "Paris", "description": "Python"},
            {
                "preferred_titles": ["Backend"],
                "preferred_locations": ["Israel"],
                "wanted_technologies": ["Rust"],
            },
        )
        == []
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(confidence=[]),
        lambda value: value.update(summary=""),
        lambda value: value.update(score=99),
        lambda value: value["assessments"].pop(),
        lambda value: value["assessments"].append(copy.deepcopy(value["assessments"][0])),
        lambda value: value["assessments"][0].update(evidence_quote="Invented CUDA expertise"),
        lambda value: value["assessments"][0].update(evidence_fact_id="someone-else"),
        lambda value: value["assessments"][0].update(status="excellent"),
        lambda value: value["assessments"][1].update(evidence_quote="Go"),
    ],
)
def test_invalid_or_ungrounded_output_rejected(mutation):
    value = assessment()
    mutation(value)
    with pytest.raises(GatewayError):
        validate_assessment(snapshot(), value)


def test_advantages_are_lower_weight_and_duplicate_requirements_removed():
    rows = criteria_for(
        {
            "description": "Go leadership CUDA",
            "normalized": {
                "requirements": ["Go", "Go", "Advantages", "CUDA"],
                "preferred": ["CUDA"],
            },
        }
    )
    assert rows == [
        {"id": "req-1", "text": "CUDA", "weight": 1},
        {"id": "req-2", "text": "Go", "weight": 3},
    ]


def test_long_posting_scores_only_deduplicated_stack():
    rows = criteria_for(
        {
            "normalized": {
                "requirements": [
                    f"Communication and leadership responsibility {i}" for i in range(40)
                ]
                + ["Build Python services", "Python and PostgreSQL", "Docker"],
                "preferred": ["Python", "Kubernetes"],
            }
        }
    )
    assert [(row["text"], row["weight"]) for row in rows] == [
        ("Docker", 3),
        ("Kubernetes", 1),
        ("PostgreSQL", 3),
        ("Python", 3),
    ]


def test_unsectioned_stack_uses_local_technology_aliases():
    rows = criteria_for({"description": "Build services with golang, k8s and postgres."})
    assert {row["text"] for row in rows} == {"Go", "Kubernetes", "PostgreSQL"}


def test_no_recognized_stack_does_not_invent_a_score():
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError, match="no recognized technologies"):
        criteria_for({"description": "Strong communication and leadership required."})
