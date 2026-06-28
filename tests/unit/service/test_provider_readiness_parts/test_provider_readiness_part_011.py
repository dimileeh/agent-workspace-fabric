"""Provider readiness runtime probe edge cases."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

import awf.service.provider_readiness as provider_readiness
from tests.unit.service.test_provider_readiness_parts.test_provider_readiness_part_001 import (
    _completed,
    _settings,
)


@pytest.mark.unit
def test_cli_auth_probe_failure_modes_are_structured_and_redacted() -> None:
    def _missing(_args: list[str], **_kwargs: object) -> Any:
        raise FileNotFoundError("missing-cli")

    def _timeout(args: list[str], **_kwargs: object) -> Any:
        raise subprocess.TimeoutExpired(args, timeout=0.1)

    def _unexpected(_args: list[str], **_kwargs: object) -> Any:
        raise RuntimeError("transport leaked sk-ant-probe-secret")

    missing = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=_missing,
        secrets=frozenset(),
    )
    timeout = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=_timeout,
        secrets=frozenset(),
    )
    unexpected = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=_unexpected,
        secrets=frozenset({"sk-ant-probe-secret"}),
    )

    assert missing["reason_code"] == "PROBE_CLI_NOT_FOUND"
    assert timeout["reason_code"] == "PROBE_AUTH_TIMEOUT"
    assert unexpected["reason_code"] == "PROBE_AUTH_ERROR"
    assert "sk-ant-probe-secret" not in json.dumps(unexpected, sort_keys=True)
    assert "<redacted>" in unexpected["detail"]


@pytest.mark.unit
def test_agent_runtime_cli_probe_failure_modes_are_structured_and_redacted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)
    settings = _settings(tmp_path)

    def _missing(_args: list[str], **_kwargs: object) -> Any:
        raise FileNotFoundError("docker")

    def _timeout(args: list[str], **_kwargs: object) -> Any:
        raise subprocess.TimeoutExpired(args, timeout=0.1)

    def _unexpected(_args: list[str], **_kwargs: object) -> Any:
        raise RuntimeError("runtime probe leaked sk-proj-runtime-secret")

    missing = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="codex",
        provider="codex",
        environ={},
        run_subprocess=_missing,
        secrets=frozenset(),
    )
    timeout = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="claude",
        provider="claude_code",
        environ={},
        run_subprocess=_timeout,
        secrets=frozenset(),
    )
    unexpected = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="gemini",
        provider="gemini",
        environ={},
        run_subprocess=_unexpected,
        secrets=frozenset({"sk-proj-runtime-secret"}),
    )

    assert missing["reason_code"] == "DOCKER_CLI_NOT_FOUND"
    assert timeout["reason_code"] == "CLAUDE_RUNTIME_CLI_PROBE_TIMEOUT"
    assert unexpected["reason_code"] == "GEMINI_RUNTIME_CLI_PROBE_ERROR"
    assert "sk-proj-runtime-secret" not in json.dumps(unexpected, sort_keys=True)
    assert "<redacted>" in unexpected["detail"]
    assert "provider_readiness.agent_runtime_cli_probe_exception" in caplog.text
    assert "sk-proj-runtime-secret" not in caplog.text


@pytest.mark.unit
def test_agent_runtime_cli_probe_reports_success_and_missing_cli(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    ok = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="opencode",
        provider="opencode",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(stdout="/usr/bin/opencode\n"),
        secrets=frozenset(),
    )
    missing = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="opencode",
        provider="opencode",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(
            returncode=127,
            stderr="opencode: not found with token sk-proj-opencode-secret",
        ),
        secrets=frozenset({"sk-proj-opencode-secret"}),
    )

    assert ok == {
        "status": "ok",
        "reason_code": "OPENCODE_RUNTIME_CLI_AVAILABLE",
        "detail": "/usr/bin/opencode",
    }
    assert missing["status"] == "fail"
    assert missing["reason_code"] == "OPENCODE_RUNTIME_CLI_NOT_FOUND"
    assert "awf-agent-runtime:latest" in missing["message"]
    assert "sk-proj-opencode-secret" not in json.dumps(missing, sort_keys=True)
    assert "<redacted>" in missing["detail"]


@pytest.mark.unit
def test_runtime_cli_probe_uses_docker_start_timeout_without_slowing_auth_probe(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    observed: dict[str, float] = {}

    def _runtime_probe(_args: list[str], **kwargs: object) -> Any:
        observed["runtime_timeout"] = float(kwargs["timeout"])
        return _completed(stdout="/usr/bin/codex\n")

    runtime = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="codex",
        provider="codex",
        environ={},
        run_subprocess=_runtime_probe,
        secrets=frozenset(),
    )

    def _auth_probe(_args: list[str], **kwargs: object) -> Any:
        observed["auth_timeout"] = float(kwargs["timeout"])
        return _completed(stdout="ok")

    auth = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=_auth_probe,
        secrets=frozenset(),
    )

    assert runtime["status"] == "ok"
    assert auth["status"] == "ok"
    assert observed["runtime_timeout"] >= 30.0
    assert observed["auth_timeout"] == provider_readiness._PROVIDER_PROBE_TIMEOUT_SECONDS


@pytest.mark.unit
def test_cli_auth_probe_reports_success_and_unusable_auth() -> None:
    success = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(stdout="ok"),
        secrets=frozenset(),
    )
    failure = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(
            returncode=1,
            stderr="invalid token sk-proj-auth-secret",
        ),
        secrets=frozenset({"sk-proj-auth-secret"}),
    )

    assert success == {"status": "ok", "reason_code": "PROBE_AUTH_OK"}
    assert failure["reason_code"] == "PROBE_AUTH_FAILED"
    assert "sk-proj-auth-secret" not in json.dumps(failure, sort_keys=True)
    assert "<redacted>" in failure["detail"]
