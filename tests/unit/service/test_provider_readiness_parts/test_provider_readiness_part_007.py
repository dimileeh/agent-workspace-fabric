"""Provider credential readiness checks for local service mode (part 7).

Split from ``test_provider_readiness_part_001`` to keep each first-party file under
the maintainability line limit; covers the non-OpenCode launch-probe and
Claude/Gemini/Grok/Codex preflight dispositions.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import awf.service.provider_readiness as provider_readiness
from awf.service.config import ServiceSettings
from awf.service.provider_readiness import (
    selected_provider_readiness_preflight,
)


def _settings(
    tmp_path: Path,
    *,
    github_token: str | None = None,
    docker_host: str | None = None,
    host_home: str | None = None,
) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}" if docker_host is None else docker_host,
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=github_token,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home") if host_home is None else host_home,
    )


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _unexpected_subprocess(args: list[str], **_kwargs: object) -> Any:
    raise AssertionError(f"unexpected subprocess call: {args}")


def _ollama_ok(url: str, *, timeout: float) -> Any:
    assert timeout > 0
    if url in {
        "http://ollama.local:11434/api/version",
        "http://localhost:11434/api/version",
    }:
        return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
    if url in {
        "http://ollama.local:11434/api/tags",
        "http://localhost:11434/api/tags",
    }:
        return SimpleNamespace(
            status_code=200,
            text='{"models":[{"name":"kimi-k2.6:cloud"}]}',
        )
    raise AssertionError(f"unexpected Ollama probe URL: {url}")


@pytest.mark.unit
def test_provider_readiness_rejects_unreachable_internal_provider(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="unsupported provider"):
        provider_readiness._check_provider_readiness(  # type: ignore[arg-type]
            "not_a_provider",
            _settings(tmp_path),
            environ={},
            host_home=tmp_path / "home",
            strict=False,
            run_subprocess=_unexpected_subprocess,
            http_get=_ollama_ok,
            secrets=frozenset(),
        )


@pytest.mark.unit
def test_selected_launch_probe_skips_when_provider_or_model_is_unavailable(
    tmp_path: Path,
) -> None:
    assert provider_readiness._selected_launch_probe(
        "codex",
        settings=_settings(tmp_path),
        provider_result={"ok": False},
        model="gpt-5.5",
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
        secrets=frozenset(),
    ) == {"status": "skipped"}
    assert provider_readiness._selected_launch_probe(
        "codex",
        settings=_settings(tmp_path),
        provider_result={"ok": True},
        model=None,
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
        secrets=frozenset(),
    ) == {"status": "skipped"}


@pytest.mark.unit
def test_selected_launch_probe_returns_runtime_failure_and_unavailable_provider(
    tmp_path: Path,
) -> None:
    runtime_failure = provider_readiness._selected_launch_probe(
        "codex",
        settings=_settings(tmp_path),
        provider_result={"ok": True},
        model="gpt-5.5",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(returncode=127, stderr="missing"),
        http_get=_ollama_ok,
        secrets=frozenset(),
    )
    unavailable = provider_readiness._selected_launch_probe(
        "docker",
        settings=_settings(tmp_path),
        provider_result={"ok": True},
        model="docker-host",
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
        secrets=frozenset(),
    )

    assert runtime_failure["reason_code"] == "CODEX_RUNTIME_CLI_NOT_FOUND"
    assert unavailable == {
        "status": "unavailable",
        "reason_code": "PROVIDER_PROBE_UNAVAILABLE",
    }


@pytest.mark.unit
def test_selected_gemini_preflight_requires_usable_non_secret_probe(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "oauth_creds.json").write_text("gemini_file_secret")
    token = "AIzaGeminiProbeSecret"

    def _run(args: list[str], **kwargs: object) -> Any:
        assert args == [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v gemini",
        ]
        assert kwargs["env"]["GEMINI_API_KEY"] == token
        return _completed(returncode=1, stdout=f"missing cli with token {token}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="gemini",
        task_policy={},
        environ={"GEMINI_API_KEY": token},
        run_subprocess=_run,
    )

    assert result["auth_status"] == "ok"
    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "GEMINI_RUNTIME_CLI_NOT_FOUND"
    assert result["blocks_launch"] is True
    serialized = json.dumps(result, sort_keys=True)
    assert token not in serialized
    assert "gemini_file_secret" not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_selected_gemini_preflight_uses_agent_runtime_cli_not_api_cli(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> Any:
        calls.append(args)
        if args[0] == "gemini":
            raise FileNotFoundError("api container gemini is absent")
        return _completed(stdout="/usr/bin/gemini\n")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="gemini",
        task_policy={},
        environ={},
        run_subprocess=_run,
    )

    assert result["provider"] == "gemini"
    assert result["readiness_status"] == "ready"
    assert result["probe_status"] == "ok"
    assert result["reason_code"] == "PROVIDER_READY"
    assert result["blocks_launch"] is False
    assert calls == [
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v gemini",
        ]
    ]


@pytest.mark.unit
def test_selected_grok_preflight_requires_xai_api_key_and_runtime_cli(
    tmp_path: Path,
) -> None:
    token = "xai-runtime-secret"
    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        calls.append(args)
        assert kwargs["env"]["XAI_API_KEY"] == token
        return _completed(stdout="/usr/local/bin/grok\n")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="grok",
        task_policy={},
        environ={"XAI_API_KEY": token},
        run_subprocess=_run,
    )

    assert result["provider"] == "grok"
    assert result["agent"] == "grok"
    assert result["model"] == "grok-build"
    assert result["readiness_status"] == "ready"
    assert result["auth_status"] == "ok"
    assert result["auth_source"] == "XAI_API_KEY"
    assert result["probe_status"] == "ok"
    assert result["reason_code"] == "PROVIDER_READY"
    assert result["blocks_launch"] is False
    assert calls == [
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v grok",
        ]
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert token not in serialized


@pytest.mark.unit
def test_selected_grok_preflight_uses_file_auth_before_xai_api_key(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "auth.json").write_text('{"token":"grok_file_secret"}')
    token = "xai-env-fallback-secret"

    def _run(args: list[str], **kwargs: object) -> Any:
        assert args[-1] == "command -v grok"
        assert kwargs["env"]["XAI_API_KEY"] == token
        return _completed(stdout="/usr/local/bin/grok\n")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="grok",
        task_policy={},
        environ={"XAI_API_KEY": token},
        run_subprocess=_run,
    )

    assert result["provider"] == "grok"
    assert result["readiness_status"] == "ready"
    assert result["auth_status"] == "ok"
    assert result["auth_source"] == "~/.grok/auth.json"
    assert result["probe_status"] == "ok"
    assert result["reason_code"] == "PROVIDER_READY"
    serialized = json.dumps(result, sort_keys=True)
    assert token not in serialized
    assert "grok_file_secret" not in serialized


@pytest.mark.unit
def test_selected_grok_preflight_blocks_missing_xai_api_key(tmp_path: Path) -> None:
    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="grok",
        task_policy={},
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    assert result["provider"] == "grok"
    assert result["readiness_status"] == "blocked"
    assert result["auth_status"] == "fail"
    assert result["probe_status"] == "skipped"
    assert result["reason_code"] == "GROK_AUTH_MISSING"
    assert result["blocks_launch"] is True


@pytest.mark.unit
def test_selected_grok_preflight_blocks_missing_runtime_cli_and_redacts_key(
    tmp_path: Path,
) -> None:
    token = "xai-missing-cli-secret"

    def _run(args: list[str], **kwargs: object) -> Any:
        assert args[-1] == "command -v grok"
        assert kwargs["env"]["XAI_API_KEY"] == token
        return _completed(returncode=1, stderr=f"grok: not found for {token}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="grok",
        task_policy={},
        environ={"XAI_API_KEY": token},
        run_subprocess=_run,
    )

    assert result["auth_status"] == "ok"
    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "GROK_RUNTIME_CLI_NOT_FOUND"
    assert result["blocks_launch"] is True
    serialized = json.dumps(result, sort_keys=True)
    assert token not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_selected_codex_preflight_blocks_when_runtime_cli_missing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token":"codex_file_secret"}')

    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args[-1] == "command -v codex"
        return _completed(returncode=1, stderr="codex: not found")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="codex",
        task_policy={},
        environ={},
        run_subprocess=_run,
    )

    assert result["auth_status"] == "ok"
    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "CODEX_RUNTIME_CLI_NOT_FOUND"
    assert result["blocks_launch"] is True
