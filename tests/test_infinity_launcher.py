"""The local launcher keeps build scripts and Rust outside Django's secret scope."""

import runpy
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts/run_infinity.py"


def test_launcher_separates_build_and_runtime_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-provider-key")
    monkeypatch.setenv("INFINITY_PROVIDER", "openai")
    monkeypatch.setenv("INFINITY_SERVICE_TOKEN", "test-service-token-" + "x" * 32)
    monkeypatch.setenv("INFINITY_TOOL_SECRET", "test-django-only-secret")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-private-document-key")
    build, execute = Mock(), Mock()
    monkeypatch.setattr("subprocess.run", build)
    monkeypatch.setattr("os.execve", execute)
    runpy.run_path(str(LAUNCHER))["main"]()
    assert "OPENAI_API_KEY" not in build.call_args.kwargs["env"]
    runtime_env = execute.call_args.args[2]
    assert runtime_env["OPENAI_API_KEY"] == "test-provider-key"
    for secret in ("INFINITY_TOOL_SECRET", "TOKEN_ENCRYPTION_KEY", "SECRET_KEY"):
        assert secret not in build.call_args.kwargs["env"]
        assert secret not in runtime_env


def test_launcher_requires_key_before_build(monkeypatch):
    monkeypatch.setenv("INFINITY_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    build = Mock()
    monkeypatch.setattr("subprocess.run", build)
    with pytest.raises(SystemExit, match="Save OPENAI_API_KEY"):
        runpy.run_path(str(LAUNCHER))["main"]()
    build.assert_not_called()


@pytest.mark.parametrize("token", ["", "x" * 31])
def test_launcher_rejects_invalid_token_before_build(monkeypatch, token):
    monkeypatch.setenv("INFINITY_PROVIDER", "fake")
    monkeypatch.setenv("INFINITY_SERVICE_TOKEN", token)
    build = Mock()
    monkeypatch.setattr("subprocess.run", build)
    with pytest.raises(SystemExit, match="at least 32 characters"):
        runpy.run_path(str(LAUNCHER))["main"]()
    build.assert_not_called()


def test_launcher_rejects_unknown_provider_before_build(monkeypatch):
    monkeypatch.setenv("INFINITY_PROVIDER", "unknown")
    build = Mock()
    monkeypatch.setattr("subprocess.run", build)
    with pytest.raises(SystemExit, match="must be openai or fake"):
        runpy.run_path(str(LAUNCHER))["main"]()
    build.assert_not_called()


def test_launcher_fake_provider_without_api_key(monkeypatch):
    monkeypatch.setenv("INFINITY_PROVIDER", "fake")
    monkeypatch.setenv("INFINITY_SERVICE_TOKEN", "x" * 32)
    monkeypatch.setenv("INFINITY_FAKE_DELAY_MS", "100")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    build, execute = Mock(), Mock()
    monkeypatch.setattr("subprocess.run", build)
    monkeypatch.setattr("os.execve", execute)
    runpy.run_path(str(LAUNCHER))["main"]()
    assert "INFINITY_FAKE_DELAY_MS" not in build.call_args.kwargs["env"]
    assert execute.call_args.args[2]["INFINITY_FAKE_DELAY_MS"] == "100"


def test_launcher_does_not_execute_after_failed_build(monkeypatch):
    monkeypatch.setenv("INFINITY_PROVIDER", "fake")
    monkeypatch.setenv("INFINITY_SERVICE_TOKEN", "x" * 32)
    build = Mock(side_effect=subprocess.CalledProcessError(1, "cargo"))
    execute = Mock()
    monkeypatch.setattr("subprocess.run", build)
    monkeypatch.setattr("os.execve", execute)
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(LAUNCHER))["main"]()
    execute.assert_not_called()
