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
        lambda *args: {
            "summary": [0],
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
        lambda *args: {
            "summary": [],
            "requirements": [999],
            "preferred": [],
            "responsibilities": [],
        },
    )
    with pytest.raises(AIUnavailable):
        normalize_with_ai("Python", normalize_description("Python"))
