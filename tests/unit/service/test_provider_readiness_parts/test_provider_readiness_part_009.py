"""Provider credential readiness checks for local service mode (part 009)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import awf.service.provider_readiness as provider_readiness
from awf.service.config import ServiceSettings
from awf.service.provider_readiness import (
    collect_agent_readiness,
)


def _settings(
    tmp_path: Path,
    *,
    github_token: str | None = None,
    docker_host: str | None = None,
    host_home: str | None = None,
    work_dir: str | None = None,
) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}" if docker_host is None else docker_host,
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work") if work_dir is None else work_dir,
        api_token=None,
        github_token=github_token,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home") if host_home is None else host_home,
    )


def _unexpected_subprocess(args: list[str], **_kwargs: object) -> Any:
    raise AssertionError(f"unexpected subprocess call: {args}")


def _ollama_ok(url: str, *, timeout: float) -> Any:
    assert timeout > 0
    if url == "http://ollama.local:11434/api/version":
        return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
    if url == "http://ollama.local:11434/api/tags":
        return SimpleNamespace(
            status_code=200,
            text='{"models":[{"name":"kimi-k2.6:cloud"}]}',
        )
    raise AssertionError(f"unexpected Ollama probe URL: {url}")


@pytest.mark.unit
def test_provider_readiness_opencode_env_only_reason_when_ollama_reachable(
    tmp_path: Path,
) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={
            "OLLAMA_API_KEY": "ollama_env_secret",
            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
        },
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["ok"] is True
    assert opencode["reason"] == "OLLAMA_ENV_AUTH_PRESENT"
    assert "ollama_env_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_opencode_http_error_detail_is_redacted(tmp_path: Path) -> None:
    def _http_get(_url: str, *, timeout: float) -> Any:
        return SimpleNamespace(status_code=401, text="bad token ghp_ollama_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={
            "OLLAMA_API_KEY": "ghp_ollama_secret",
            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
        },
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["reason"] == "OLLAMA_HOST_UNREACHABLE"
    serialized = json.dumps(payload, sort_keys=True)
    assert "ghp_ollama_secret" not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_ollama_http_probe_all_http_failures_log_redacted_terminal_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)

    def _http_get(_url: str, *, timeout: float) -> Any:
        assert timeout > 0
        return SimpleNamespace(status_code=503, text="busy sk-proj-ollama-secret")

    result = provider_readiness._probe_ollama(
        ("http://ollama.local:11434/api/version",),
        http_get=_http_get,
        secrets=frozenset({"sk-proj-ollama-secret"}),
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["ok"] is False
    assert "HTTP 503: busy <redacted>" in serialized
    assert "provider_readiness.ollama_probe_exception" in caplog.text
    assert "HTTP 503: busy <redacted>" in caplog.text
    assert "Traceback" not in caplog.text
    assert "sk-proj-ollama-secret" not in serialized
    assert "sk-proj-ollama-secret" not in caplog.text


@pytest.mark.unit
def test_ollama_http_probe_exception_logs_redacted_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)

    def _http_get(_url: str, *, timeout: float) -> Any:
        assert timeout > 0
        raise httpx.ConnectError("transport failed for sk-proj-ollama-secret")

    result = provider_readiness._probe_ollama(
        ("http://ollama.local:11434/api/version",),
        http_get=_http_get,
        secrets=frozenset({"sk-proj-ollama-secret"}),
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["ok"] is False
    assert "ConnectError: transport failed for <redacted>" in serialized
    assert "provider_readiness.ollama_probe_exception" in caplog.text
    assert "Traceback" in caplog.text
    assert "ConnectError: transport failed for <redacted>" in caplog.text
    assert "sk-proj-ollama-secret" not in serialized
    assert "sk-proj-ollama-secret" not in caplog.text


@pytest.mark.unit
def test_ollama_http_probe_invalid_url_is_structured_failure_not_raise() -> None:
    """A syntactically invalid probe URL raises ``httpx.InvalidURL`` (not an
    ``httpx.HTTPError`` subclass). It must be captured as a structured, redacted
    readiness failure — an operator configuration error — rather than escape as
    an unhandled raise from service readiness/doctor."""

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        raise httpx.InvalidURL(f"malformed URL with sk-proj-ollama-secret: {url}")

    result = provider_readiness._probe_ollama(
        ("http://ollama.local:11434/api/version",),
        http_get=_http_get,
        secrets=frozenset({"sk-proj-ollama-secret"}),
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["ok"] is False
    assert "InvalidURL:" in serialized
    assert "sk-proj-ollama-secret" not in serialized
    assert "<redacted>" in result["detail"]


@pytest.mark.unit
def test_ollama_model_probe_invalid_url_is_structured_failure_not_raise() -> None:
    """The model availability probe shares the ``http_get`` config-URL path, so an
    ``httpx.InvalidURL`` must likewise resolve to ``OLLAMA_MODEL_PROBE_FAILED``
    rather than escape as an unhandled raise."""

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        raise httpx.InvalidURL(f"malformed URL with sk-proj-ollama-secret: {url}")

    result = provider_readiness._probe_ollama_model(
        ("http://ollama.local:11434/api/tags",),
        model="llama3",
        http_get=_http_get,
        secrets=frozenset({"sk-proj-ollama-secret"}),
    )

    assert result["reason_code"] == "OLLAMA_MODEL_PROBE_FAILED"
    assert "InvalidURL:" in json.dumps(result, sort_keys=True)
    assert "sk-proj-ollama-secret" not in json.dumps(result, sort_keys=True)
    assert "<redacted>" in result["detail"]


@pytest.mark.unit
def test_ollama_model_probe_reports_missing_model_and_transport_failures() -> None:
    calls: list[str] = []

    assert provider_readiness._probe_ollama_model(
        ("http://ollama.local:11434/api/tags",),
        model=None,
        http_get=lambda _url, *, timeout: _ollama_ok(
            "http://ollama.local:11434/api/tags", timeout=timeout
        ),
        secrets=frozenset(),
    ) == {
        "status": "fail",
        "reason_code": "MODEL_NOT_SELECTED",
        "message": "No OpenCode/Ollama model was selected for launch.",
    }

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        calls.append(url)
        if url == "http://primary.local/api/tags":
            raise httpx.ConnectError("connect failed")
        return SimpleNamespace(status_code=503, text="busy sk-proj-ollama-secret")

    result = provider_readiness._probe_ollama_model(
        ("http://primary.local/api/tags", "http://secondary.local/api/tags"),
        model="llama3",
        http_get=_http_get,
        secrets=frozenset({"sk-proj-ollama-secret"}),
    )

    assert result["reason_code"] == "OLLAMA_MODEL_PROBE_FAILED"
    assert calls == ["http://primary.local/api/tags", "http://secondary.local/api/tags"]
    assert "sk-proj-ollama-secret" not in json.dumps(result, sort_keys=True)
    assert "<redacted>" in result["detail"]


@pytest.mark.unit
def test_ollama_model_probe_checks_fallback_tags_urls_before_missing() -> None:
    calls: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        calls.append(url)
        if url == "http://host.docker.internal:11434/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"other-model:latest"}]}',
            )
        if url == "http://localhost:11434/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"llama3:latest"}]}',
            )
        raise AssertionError(f"unexpected Ollama tags URL: {url}")

    result = provider_readiness._probe_ollama_model(
        (
            "http://host.docker.internal:11434/api/tags",
            "http://localhost:11434/api/tags",
        ),
        model="llama3",
        http_get=_http_get,
        secrets=frozenset(),
    )

    assert result == {"status": "ok", "reason_code": "OLLAMA_MODEL_AVAILABLE"}
    assert calls == [
        "http://host.docker.internal:11434/api/tags",
        "http://localhost:11434/api/tags",
    ]


@pytest.mark.unit
def test_ollama_model_probe_records_recovered_failure_debug_when_available(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://primary.local/api/tags":
            raise httpx.ConnectError("connect failed for sk-proj-ollama-secret")
        return SimpleNamespace(status_code=200, text='{"models":[{"name":"llama3:latest"}]}')

    result = provider_readiness._probe_ollama_model(
        ("http://primary.local/api/tags", "http://secondary.local/api/tags"),
        model="llama3",
        http_get=_http_get,
        secrets=frozenset({"sk-proj-ollama-secret"}),
    )

    assert result == {
        "status": "ok",
        "reason_code": "OLLAMA_MODEL_AVAILABLE",
        "debug": {
            "recovered_failures": [
                {
                    "url": "http://primary.local/api/tags",
                    "status": "exception",
                    "detail": "ConnectError: connect failed for <redacted>",
                }
            ]
        },
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "provider_readiness.ollama_model_probe_exception" not in caplog.text
    assert "Traceback" not in caplog.text
    assert "sk-proj-ollama-secret" not in serialized
    assert "sk-proj-ollama-secret" not in caplog.text

    caplog.clear()

    def _http_error_then_success(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://primary.local/api/tags":
            return SimpleNamespace(status_code=500, text="error for sk-proj-ollama-secret")
        return SimpleNamespace(status_code=200, text='{"models":[{"name":"llama3:latest"}]}')

    http_error_result = provider_readiness._probe_ollama_model(
        ("http://primary.local/api/tags", "http://secondary.local/api/tags"),
        model="llama3",
        http_get=_http_error_then_success,
        secrets=frozenset({"sk-proj-ollama-secret"}),
    )

    assert http_error_result == {
        "status": "ok",
        "reason_code": "OLLAMA_MODEL_AVAILABLE",
        "debug": {
            "recovered_failures": [
                {
                    "url": "http://primary.local/api/tags",
                    "status": "http_error",
                    "status_code": 500,
                    "detail": "HTTP 500: error for <redacted>",
                }
            ]
        },
    }
    serialized = json.dumps(http_error_result, sort_keys=True)
    assert "provider_readiness.ollama_model_probe_exception" not in caplog.text
    assert "Traceback" not in caplog.text
    assert "sk-proj-ollama-secret" not in serialized
    assert "sk-proj-ollama-secret" not in caplog.text
