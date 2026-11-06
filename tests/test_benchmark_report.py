import json
import stat

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
