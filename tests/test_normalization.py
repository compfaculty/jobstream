from jobs.services.normalization import normalize_description, text_blocks


def test_sections_and_marketing_exclusion():
    value = normalize_description("""<p>Build backend services.</p><h2>Requirements</h2>
    <ul><li>Python and PostgreSQL</li></ul><h2>Nice to have</h2><p>PyTorch</p>
    <h2>Benefits</h2><p>Our amazing AI future</p>""")
    assert value["summary"] == "Build backend services."
    assert value["requirements"] == ["Python and PostgreSQL"]
    assert value["preferred"] == ["PyTorch"]
    assert value["technologies"] == ["Python", "PostgreSQL", "PyTorch"]
    assert "amazing" not in str(value["requirements"])


def test_plain_description_unknown_sections():
    value = normalize_description("Python backend engineer. Remote work subject to country rules.")
    assert value["requirements"] == []
    assert value["preferred"] == []
    assert value["technologies"] == ["Python"]


def test_scripts_removed_and_line_boundaries_preserved():
    assert (
        text_blocks("<script>bad()</script><h2>Requirements</h2><p>Linux</p>")
        == "Requirements\nLinux"
    )


def test_ai_sections_must_reference_original_lines(monkeypatch):
    from jobs.services.normalization import normalize_with_ai

    description = "Build backend services.\nYou need Python.\nHelpful: Go."
    monkeypatch.setattr(
        "jobs.services.ai.request_json",
        lambda *args, **kwargs: {
            "summary": [0],
            "product": [],
            "requirements": [1],
            "preferred": [2],
            "responsibilities": [],
        },
    )
    result = normalize_with_ai(description, normalize_description(description))
    assert result["requirements"] == ["You need Python."]
    assert result["preferred"] == ["Helpful: Go."]


def test_ai_sections_reject_out_of_range_lines(monkeypatch):
    import pytest

    from jobs.services.ai import AIUnavailable
    from jobs.services.normalization import normalize_with_ai

    monkeypatch.setattr(
        "jobs.services.ai.request_json",
        lambda *args, **kwargs: {
            "summary": [],
            "product": [],
            "requirements": [999],
            "preferred": [],
            "responsibilities": [],
        },
    )
    with pytest.raises(AIUnavailable):
        normalize_with_ai("Python", normalize_description("Python"))


def test_nodejs_and_required_vs_preferred_technologies():
    result = normalize_description("Requirements\nNodeJS and Python\nNice to have\nRust and Golang")
    assert result["required_technologies"] == ["Python", "Node.js"]
    assert result["technologies"] == ["Python", "Go", "Rust", "Node.js"]
    assert normalize_description("node.js service")["technologies"] == ["Node.js"]
    assert normalize_description("We go to the office")["technologies"] == []


def test_role_summary_replaces_company_marketing_and_advantages_are_optional():
    value = normalize_description("""We are a world-leading visionary cloud company.
Responsibilities
Build a low-latency model inference platform.
Requirements
Go and Kubernetes
Advantages
CUDA and Triton serving
Benefits
Free lunch""")
    assert value["summary"] == "Build a low-latency model inference platform."
    assert value["requirements"] == ["Go and Kubernetes"]
    assert value["preferred"] == ["CUDA and Triton serving"]
    assert "CUDA" in value["technologies"]
    assert "CUDA" not in value["required_technologies"]


def test_ai_prompt_and_product_use_original_lines_with_separate_model(monkeypatch, settings):
    from jobs.services.normalization import normalize_with_ai

    settings.JOB_NORMALIZATION_MODEL = "gpt-6-luna"
    settings.JOB_NORMALIZATION_REASONING_EFFORT = "none"
    calls = []

    def provider(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "summary": [1],
            "product": [0],
            "requirements": [2],
            "preferred": [3],
            "responsibilities": [1],
        }

    monkeypatch.setattr("jobs.services.ai.request_json", provider)
    result = normalize_with_ai(
        "Our product detects network threats.\nBuild inference services.\n"
        "Go required.\nCUDA is optional.",
        {},
    )
    assert result["product"] == "Our product detects network threats."
    assert result["summary"] == "Build inference services."
    assert result["model"] == "gpt-6-luna"
    assert calls[0][1]["reasoning_effort"] == "none"
    assert calls[0][1]["max_output_tokens"] == 800
    assert "job-description analyst" in calls[0][0][4]


def test_ai_rejects_overlapping_required_and_optional_lines(monkeypatch):
    import pytest

    from jobs.services.ai import AIUnavailable
    from jobs.services.normalization import normalize_with_ai

    monkeypatch.setattr(
        "jobs.services.ai.request_json",
        lambda *args, **kwargs: {
            "summary": [],
            "product": [],
            "requirements": [0],
            "preferred": [0],
            "responsibilities": [],
        },
    )
    with pytest.raises(AIUnavailable, match="ai_invalid_sections"):
        normalize_with_ai("Python", {})


def test_ai_needed_when_either_essential_section_is_missing():
    from jobs.services.normalization import needs_ai_normalization

    assert needs_ai_normalization({"requirements": ["Go"], "responsibilities": []})
    assert needs_ai_normalization({"requirements": [], "responsibilities": ["Build services"]})
    assert not needs_ai_normalization({"requirements": ["Go"], "responsibilities": ["Build"]})


def test_normalization_refresh_preserves_originals_and_operator_overrides(db, job):
    from django.core.management import call_command

    original = job.description
    call_command("normalize_jobs", job=job.pk, apply=True)
    job.refresh_from_db()
    assert job.description == original
    assert job.normalized["version"] == "sections-v3"
    job.content_overrides = {"normalized": {"summary": "Operator reviewed"}}
    job.normalized = {"summary": "Operator reviewed"}
    job.save()
    call_command("normalize_jobs", job=job.pk, apply=True)
    job.refresh_from_db()
    assert job.normalized == {"summary": "Operator reviewed"}
