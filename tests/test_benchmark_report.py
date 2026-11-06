import json
import stat

import pytest

from jobs.management.commands.benchmark_pilot import write_report


def test_default_reports_are_unique_private_and_complete(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    first = write_report({"passed": False}, None)
    second = write_report({"passed": True}, None)
    assert first != second
    assert first.parent == tmp_path
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert json.loads(first.read_text()) == {"passed": False}
    assert json.loads(second.read_text()) == {"passed": True}


def test_explicit_report_path_remains_supported(tmp_path):
    destination = tmp_path / "chosen.json"
    assert write_report({"passed": False}, str(destination)) == destination
    assert json.loads(destination.read_text()) == {"passed": False}


@pytest.mark.django_db
@pytest.mark.parametrize("failure", [None, "posting", "comparison"])
def test_live_gate_requires_every_case(tmp_path, monkeypatch, failure):
    import io
    import json

    from django.core.management import call_command

    from jobs.management.commands import benchmark_pilot as benchmark
    from jobs.services.ai import AIUnavailable

    public = [
        {
            "id": i,
            "reviewed": True,
            "description": str(i),
            "labels": [{"line": 0, "field": "requirements"}],
        }
        for i in range(100)
    ]
    comparisons = [
        {"id": i, "requirements": ["Python", "Linux"], "cv": str(i), "expected": ["met", "met"]}
        for i in range(30)
    ]
    (tmp_path / "public_jobs.json").write_text(json.dumps(public))
    (tmp_path / "comparisons.json").write_text(json.dumps(comparisons))
    monkeypatch.setattr(benchmark, "normalize_description", lambda text: {})
    monkeypatch.setattr(benchmark, "needs_ai_normalization", lambda value: True)
    monkeypatch.setattr(benchmark, "text_blocks", lambda text: text)

    def normalize(text, deterministic):
        if failure == "posting" and text == "99":
            raise AIUnavailable("ai_daily_budget_exceeded")
        return {"requirements": [text]}

    def compare(owner, purpose, snapshot, *args, **kwargs):
        if failure == "comparison" and snapshot["facts"][0]["text"] == "29":
            raise AIUnavailable("ai_daily_budget_exceeded")
        return {"assessments": [{"requirement_id": f"r{i}", "status": "met"} for i in range(2)]}

    monkeypatch.setattr(benchmark, "normalize_with_ai", normalize)
    monkeypatch.setattr(benchmark, "request_json", compare)
    # This test isolates gate coverage; evidence validation has its own contract tests.
    monkeypatch.setattr(benchmark, "validate_assessment", lambda *args: None)
    report = tmp_path / "report.json"
    call_command(
        "benchmark_pilot",
        live=True,
        fixtures=str(tmp_path),
        report=str(report),
        stdout=io.StringIO(),
    )
    result = json.loads(report.read_text())
    assert result["passed"] is (failure is None)
    assert result["completed_postings"] == (99 if failure == "posting" else 100)
    assert result["completed_comparisons"] == (29 if failure == "comparison" else 30)
    assert result["comparison_checks"] >= 30


@pytest.mark.django_db
def test_charge_observation_excludes_other_contexts_and_restores_after_failure():
    from contextvars import Context

    from jobs.services.ai_costs import observe_charges, reserve

    with pytest.raises(RuntimeError), observe_charges() as observed:
        own = reserve("benchmark", "gpt-6-luna", "test", 1, 1)
        reserve("benchmark", "gpt-6-luna", "test", 1, 1)
        Context().run(reserve, "unrelated", "gpt-6-luna", "test", 1, 1)
        assert observed == {own.pk}
        raise RuntimeError("stop")
    reserve("after", "gpt-6-luna", "test", 1, 1)
    assert observed == {own.pk}
