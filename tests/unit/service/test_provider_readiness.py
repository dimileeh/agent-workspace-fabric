"""Coverage for the single-provider readiness seam used by provider setup (T07).

``check_single_provider_readiness`` is an additive integration point: it probes
exactly one provider with the same bounded, secret-redacting checks
``collect_agent_readiness`` runs, without touching the other providers.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.service.config import ServiceSettings
from awf.service.provider_readiness import (
    check_single_provider_readiness,
    default_subprocess_runner,
)


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )


def _unexpected_subprocess(args: list[str], **_kwargs: object) -> Any:
    raise AssertionError(f"unexpected subprocess call: {args}")


@pytest.mark.unit
def test_default_subprocess_runner_delegates_to_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public seam returns the bounded runner that calls ``subprocess.run``.

    Callers outside this package reuse this factory instead of importing the
    private ``_run_subprocess`` helper, so the contract must stay stable: invoking
    the returned runner forwards verbatim to ``subprocess.run``.
    """
    from awf.service import provider_readiness_helpers

    captured: dict[str, Any] = {}
    completed = SimpleNamespace(returncode=0, stdout="", stderr="")

    def _subprocess_run(args: list[str], **kwargs: Any) -> Any:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return completed

    monkeypatch.setattr(provider_readiness_helpers.subprocess, "run", _subprocess_run)

    runner = default_subprocess_runner()
    result = runner(
        ["gh", "auth", "status"],
        check=False,
        capture_output=True,
        text=True,
        timeout=1.5,
        env={},
    )

    assert result is completed
    assert captured["args"] == ["gh", "auth", "status"]
    assert captured["kwargs"]["timeout"] == 1.5


@pytest.mark.unit
def test_single_provider_seam_probes_only_requested_provider(tmp_path: Path) -> None:
    """Probing codex via the seam touches no GitHub subprocess and redacts secrets."""
    result = check_single_provider_readiness(
        _settings(tmp_path),
        provider="codex",
        environ={"OPENAI_API_KEY": "sk-secret-value"},
        run_subprocess=_unexpected_subprocess,
    )

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert "sk-secret-value" not in str(result)


@pytest.mark.unit
def test_single_provider_seam_http_probe_is_bounded(tmp_path: Path) -> None:
    """The Ollama readiness probe receives a positive (bounded) timeout."""
    seen: dict[str, float] = {}

    def _http_get(url: str, *, timeout: float) -> Any:
        seen[url] = timeout
        if url.endswith("/api/version"):
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        return SimpleNamespace(status_code=200, text='{"models":[]}')

    result = check_single_provider_readiness(
        _settings(tmp_path),
        provider="opencode",
        environ={"OLLAMA_API_KEY": "ollama-xyz"},
        http_get=_http_get,
    )

    assert result["ok"] is True
    assert seen and all(timeout > 0 for timeout in seen.values())


@pytest.mark.unit
def test_claude_code_readiness_includes_mount_propagation(tmp_path: Path) -> None:
    host_home = tmp_path / "home"
    host_home.mkdir()
    (host_home / ".claude").mkdir()
    result = check_single_provider_readiness(
        _settings(tmp_path),
        provider="claude_code",
        environ={
            "ANTHROPIC_API_KEY": "sk-test-value",
            "AWF_WORK_DIR_BIND_PROPAGATION": "rprivate",
        },
        run_subprocess=_unexpected_subprocess,
    )
    assert result["ok"] is True
    assert result.get("mount_propagation") == "rprivate"


@pytest.mark.unit
def test_claude_code_readiness_omits_mount_propagation_when_absent(tmp_path: Path) -> None:
    host_home = tmp_path / "home"
    host_home.mkdir()
    (host_home / ".claude").mkdir()
    result = check_single_provider_readiness(
        _settings(tmp_path),
        provider="claude_code",
        environ={"ANTHROPIC_API_KEY": "sk-test-value"},
        run_subprocess=_unexpected_subprocess,
    )
    assert result["ok"] is True
    assert "mount_propagation" not in result
