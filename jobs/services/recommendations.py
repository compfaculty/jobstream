"""Read-only recommendations from current, owner-scoped CV evaluations."""

from django.db.models import CharField, F, Func, OuterRef, Subquery
from django.db.models.functions import JSONObject

from jobs.models import AgentExecution, Document, ResumeProfile

from .evaluations import POLICY_FIELDS, VERSION
from .preference_matching import preferences_for

MINIMUM_SCORE = 70
JOB_FIELDS = ("title", "company", "description", "location", "normalized")


def sources_for(owner, selected: str = "") -> dict:
    documents = list(Document.objects.filter(owner=owner).order_by("-created_at", "-pk"))
    profiles = list(
        ResumeProfile.objects.filter(owner=owner, document__owner=owner)
        .defer("facts", "contact")
        .order_by("-confirmed_at", "-pk")
    )
    if not selected:
        if documents:
            selected = f"document:{documents[0].pk}"
        elif profiles:
            selected = f"profile:{profiles[0].pk}"
    choices = {f"document:{item.pk}": item for item in documents}
    choices.update({f"profile:{item.pk}": item for item in profiles})
    source = choices.get(selected)
    return {
        "recommendation_documents": [
            {"value": f"document:{d.pk}", "name": d.name} for d in documents
        ],
        "recommendation_profiles": [
            {"value": f"profile:{p.pk}", "name": p.name, "pk": p.pk} for p in profiles
        ],
        "selected_cv": selected if source else "",
        "recommendation_source": source,
        "recommendation_source_invalid": bool(selected and source is None),
        "recommendation_minimum": MINIMUM_SCORE,
    }


def recommended_jobs(jobs, owner, source):
    if source is None:
        return jobs.none()
    policy = preferences_for(owner.pk)
    current_policy = {name: getattr(policy, name) for name in POLICY_FIELDS}
    scores = AgentExecution.objects.filter(
        owner=owner,
        job_id=OuterRef("pk"),
        status="completed",
        version=VERSION,
        snapshot__assessment_version=2,
        snapshot__preferences=current_policy,
        snapshot__preference_blocks=[],
        snapshot__job=JSONObject(**{name: OuterRef(name) for name in JOB_FIELDS}),
    )
    if isinstance(source, Document):
        scores = scores.filter(
            document=source,
            profile__isnull=True,
            snapshot__input_source__kind="uploaded_pdf",
            snapshot__input_source__document_id=source.pk,
            snapshot__input_source__sha256=source.sha256,
        )
    else:
        scores = scores.filter(
            profile=source, document_id=source.document_id, snapshot__facts=source.facts
        )
    # JSON type checks prevent strings such as "90" from sorting above numeric values.
    scores = scores.annotate(
        score_type=Func(F("result__match_score"), function="jsonb_typeof", output_field=CharField())
    ).filter(score_type="number", result__match_score__gte=0, result__match_score__lte=100)
    latest = scores.order_by("-created_at", "-pk").values("result__match_score")[:1]
    return (
        jobs.filter(active=True)
        .annotate(recommendation_score=Subquery(latest))
        .filter(recommendation_score__gte=MINIMUM_SCORE)
        .order_by("-recommendation_score", "-discovered_at", "-pk")
    )
