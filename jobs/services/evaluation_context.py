"""Compact paid model input; the original immutable snapshot stays in Django."""


def runtime_context(snapshot: dict) -> dict:
    if snapshot.get("runtime_context_version") != 1:
        return snapshot
    job = snapshot["job"]
    normalized = job.get("normalized") or {}
    return {
        "assessment_version": snapshot["assessment_version"],
        "job": {
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "summary": normalized.get("summary", ""),
            "product": normalized.get("product", ""),
        },
        "criteria": [
            c
            for c in snapshot["criteria"]
            if c["id"] not in {a["requirement_id"] for a in snapshot.get("exact_assessments", [])}
        ],
        "facts": [{**fact, "text": " ".join(fact["text"].split())} for fact in snapshot["facts"]],
        "fit": snapshot["fit"],
    }
