"""Provider credential readiness checks for local service mode (part 8).

Split from ``test_provider_readiness_part_002`` to keep each first-party file under
the maintainability line limit; covers the Ollama model-probe disposition,
detail truncation, and the default subprocess/HTTP wrappers and token redaction.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import awf.service.provider_readiness as provider_readiness
import awf.service.provider_readiness_helpers as provider_readiness_helpers
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


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.unit
def test_ollama_model_probe_reports_missing_model_with_probe_failures() -> None:
    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://primary.local/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"other-model:latest"}]}',
            )
        return SimpleNamespace(status_code=503, text="busy")

    result = provider_readiness._probe_ollama_model(
        ("http://primary.local/api/tags", "http://secondary.local/api/tags"),
        model="llama3",
        http_get=_http_get,
        secrets=frozenset(),
    )

    assert result["reason_code"] == "OLLAMA_MODEL_NOT_AVAILABLE"
    assert result["detail"] == (
        "selected=llama3; available_count=1; "
        "probe_failures=http://secondary.local/api/tags: HTTP 503: busy"
    )


@pytest.mark.unit
def test_ollama_model_probe_missing_model_redacts_before_truncating_detail() -> None:
    secret = "LEAKME-sensitive-ollama-secret-value"

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://primary.local/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"other-model:latest"}]}',
            )
        return SimpleNamespace(status_code=503, text=("x" * 120) + secret + "-tail")

    result = provider_readiness._probe_ollama_model(
        ("http://primary.local/api/tags", "http://secondary.local/api/tags"),
        model="llama3",
        http_get=_http_get,
        secrets=frozenset({secret}),
    )

    assert result["reason_code"] == "OLLAMA_MODEL_NOT_AVAILABLE"
    assert secret not in result["detail"]
    assert "LEAKME" not in result["detail"]
    assert "<redacted>" in result["detail"]


@pytest.mark.unit
def test_ollama_model_probe_failure_redacts_before_truncating_detail() -> None:
    secret = "LEAKME-sensitive-ollama-secret-value"

    def _http_get(_url: str, *, timeout: float) -> Any:
        assert timeout > 0
        return SimpleNamespace(status_code=503, text=("x" * 160) + secret + "-tail")

    result = provider_readiness._probe_ollama_model(
        ("http://primary.local/api/tags",),
        model="llama3",
        http_get=_http_get,
        secrets=frozenset({secret}),
    )

    assert result["reason_code"] == "OLLAMA_MODEL_PROBE_FAILED"
    assert secret not in result["detail"]
    assert "LEAKME" not in result["detail"]
    assert "<redacted>" in result["detail"]


@pytest.mark.unit
def test_ollama_model_probe_logs_exception_after_missing_model_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://primary.local/api/tags":
            raise httpx.ConnectError("connect failed for sk-proj-ollama-secret")
        return SimpleNamespace(
            status_code=200,
            text='{"models":[{"name":"other-model:latest"}]}',
        )

    result = provider_readiness._probe_ollama_model(
        ("http://primary.local/api/tags", "http://secondary.local/api/tags"),
        model="llama3",
        http_get=_http_get,
        secrets=frozenset({"sk-proj-ollama-secret"}),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_NOT_AVAILABLE"
    assert (
        "probe_failures=http://primary.local/api/tags: ConnectError: connect failed for <redacted>"
        in result["detail"]
    )
    assert "provider_readiness.ollama_model_probe_exception" in caplog.text
    assert "Traceback" in caplog.text
    assert "ConnectError: connect failed for <redacted>" in caplog.text
    assert "sk-proj-ollama-secret" not in caplog.text
    assert "sk-proj-ollama-secret" not in json.dumps(result, sort_keys=True)


@pytest.mark.unit
def test_ollama_model_probe_rejects_invalid_json() -> None:
    def _http_get(_url: str, *, timeout: float) -> Any:
        assert timeout > 0
        return SimpleNamespace(status_code=200, text="{not-json")

    result = provider_readiness._probe_ollama_model(
        ("http://ollama.local/api/tags",),
        model="llama3",
        http_get=_http_get,
        secrets=frozenset(),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PROBE_FAILED"
    assert "invalid JSON from Ollama /api/tags" in result["detail"]


@pytest.mark.unit
def test_ollama_model_candidate_and_name_helpers_handle_sparse_shapes() -> None:
    assert provider_readiness_helpers._ollama_model_candidates(None) == set()
    assert provider_readiness_helpers._ollama_model_candidates("   ") == set()
    assert provider_readiness_helpers._ollama_model_candidates("openai/gpt-oss") == {
        "openai/gpt-oss",
        "openai/gpt-oss:latest",
    }
    assert provider_readiness_helpers._ollama_model_candidates("ollama/") == {
        "ollama/",
        "ollama/:latest",
    }
    assert provider_readiness_helpers._ollama_model_candidates("llama3:8b") == {"llama3:8b"}

    assert provider_readiness_helpers._ollama_model_names(None) == set()
    assert provider_readiness_helpers._ollama_model_names({"models": "bad-shape"}) == set()
    assert provider_readiness_helpers._ollama_model_names(
        {
            "models": [
                "llama3:latest",
                "",
                42,
                {},
                {"model": "mistral:7b"},
                {"name": "qwen:14b"},
            ]
        }
    ) == {"llama3:latest", "mistral:7b", "qwen:14b"}


@pytest.mark.unit
def test_provider_readiness_truncates_verbose_details(tmp_path: Path) -> None:
    def _run(_args: list[str], **_kwargs: object) -> Any:
        return _completed(returncode=1, stderr="failure " * 50)

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_GITHUB_TOKEN": "ghp_verbose_secret"},
        run_subprocess=_run,
    )

    detail = payload["providers"]["github"]["detail"]
    assert isinstance(detail, str)
    assert len(detail) == 240
    assert detail.endswith("\N{HORIZONTAL ELLIPSIS}")


@pytest.mark.unit
def test_provider_readiness_default_subprocess_and_http_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _completed(stdout="ok")
    calls: list[tuple[list[str], float]] = []

    def _subprocess_run(args: list[str], **kwargs: object) -> Any:
        calls.append((args, kwargs["timeout"]))
        return completed

    def _httpx_get(url: str, *, timeout: float) -> Any:
        assert url == "http://example.test/api/version"
        assert timeout == 1.5
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(provider_readiness_helpers.subprocess, "run", _subprocess_run)
    monkeypatch.setattr(provider_readiness_helpers.httpx, "get", _httpx_get)

    assert (
        provider_readiness_helpers._run_subprocess(
            ["gh", "auth", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
            env={},
        )
        is completed
    )
    assert calls == [(["gh", "auth", "status"], 1.5)]
    assert (
        provider_readiness_helpers._http_get("http://example.test/api/version", timeout=1.5).text
        == "ok"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "secret",
    [
        "ghp_providerreadinesssecret",
        "gho_providerreadinesssecret",
        "github_pat_providerreadinesssecret",
        "sk-proj-provider-readiness-secret",
        "sk-ant-provider-readiness-secret",
        "sk-providerReadinessSecret1234567890",
        "AIzaProviderReadinessSecret",
        "xoxb-provider-readiness-secret",
    ],
)
def test_provider_readiness_redacts_known_token_patterns(secret: str) -> None:
    assert provider_readiness._redact(f"token {secret}", frozenset()) == "token <redacted>"
