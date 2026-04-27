"""Provider credential readiness checks for local service mode."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import awf.service.provider_readiness as provider_readiness
from awf.service.config import ServiceSettings
from awf.service.provider_readiness import (
    ProviderReadinessError,
    collect_agent_readiness,
    validate_provider_names,
)


def _settings(
    tmp_path: Path,
    *,
    github_token: str | None = None,
    host_home: str | None = None,
) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="sqlite+aiosqlite:///:memory:",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
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
    assert url == "http://ollama.local:11434/api/version"
    return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')


@pytest.mark.unit
def test_provider_readiness_validates_aliases_and_rejects_unknown() -> None:
    assert validate_provider_names(["claude", "opencode", ""]) == {
        "claude_code",
        "opencode",
    }

    with pytest.raises(ProviderReadinessError, match="unknown provider"):
        validate_provider_names(["github", "bogus"])


@pytest.mark.unit
def test_provider_readiness_all_green(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".gemini").mkdir()
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".ollama").mkdir()
    (home / ".ollama" / "config.json").write_text("ollama-file-secret")
    github_secret = "ghp_green_secret"
    anthropic_secret = "sk-ant-green-secret"
    gemini_secret = "gemini_green_secret"
    ollama_secret = "ollama_green_secret"
    env = {
        "AWF_GITHUB_TOKEN": github_secret,
        "ANTHROPIC_API_KEY": anthropic_secret,
        "GEMINI_API_KEY": gemini_secret,
        "OLLAMA_API_KEY": ollama_secret,
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
    }
    subprocess_calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        subprocess_calls.append(args)
        assert args == ["gh", "auth", "status", "--hostname", "github.com"]
        assert github_secret not in args
        subprocess_env = kwargs["env"]
        assert isinstance(subprocess_env, dict)
        assert subprocess_env["GH_TOKEN"] == github_secret
        return _completed(stdout="logged in\n")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ=env,
        run_subprocess=_run,
        http_get=_ollama_ok,
    )

    assert payload["status"] == "ok"
    providers = payload["providers"]
    assert set(providers) == {"github", "claude_code", "gemini", "opencode"}
    assert all(provider["ok"] is True for provider in providers.values())
    assert providers["github"]["capabilities"] == ["pr_create", "comment", "merge"]
    assert subprocess_calls == [["gh", "auth", "status", "--hostname", "github.com"]]
    serialized = json.dumps(payload, sort_keys=True)
    for secret in (github_secret, anthropic_secret, gemini_secret, ollama_secret):
        assert secret not in serialized


@pytest.mark.unit
def test_provider_readiness_missing_github_token_warns_by_default(tmp_path: Path) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    github = payload["providers"]["github"]
    assert github["status"] == "warn"
    assert github["ok"] is False
    assert github["reason"] == "GITHUB_TOKEN_ENV_MISSING"
    assert payload["status"] == "ok"


@pytest.mark.unit
def test_provider_readiness_github_strict_missing_token_fails(tmp_path: Path) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        strict_providers={"github"},
        run_subprocess=_unexpected_subprocess,
    )

    github = payload["providers"]["github"]
    assert github["status"] == "fail"
    assert github["ok"] is False
    assert github["reason"] == "GITHUB_TOKEN_ENV_MISSING"
    assert payload["status"] == "fail"
    assert payload["strict_providers"] == ["github"]


@pytest.mark.unit
def test_provider_readiness_keyring_only_github_warning_is_actionable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    gh_config = home / ".config" / "gh"
    gh_config.mkdir(parents=True)
    (gh_config / "hosts.yml").write_text("oauth_token: ghp_file_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_KEYRING_ONLY_NOT_VISIBLE_IN_COMPOSE"
    assert "AWF_GITHUB_TOKEN" in str(github)
    assert "GH_TOKEN" in str(github)
    assert "ghp_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_empty_host_home_defaults_to_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"token":"claude_file_secret"}')
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))

    payload = collect_agent_readiness(
        _settings(tmp_path, host_home=""),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["ok"] is True
    assert claude["reason"] == "CLAUDE_FILE_AUTH_PRESENT"
    assert "claude_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_github_settings_token_cli_missing(tmp_path: Path) -> None:
    github_secret = "github_pat_settings_secret"

    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args == ["gh", "auth", "status", "--hostname", "github.com"]
        raise FileNotFoundError("gh")

    payload = collect_agent_readiness(
        _settings(tmp_path, github_token=github_secret),
        environ={},
        run_subprocess=_run,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_CLI_NOT_FOUND"
    assert github["signals"] == ["AWF_GITHUB_TOKEN"]
    assert github_secret not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_github_auth_timeout(tmp_path: Path) -> None:
    def _run(args: list[str], **_kwargs: object) -> Any:
        raise subprocess.TimeoutExpired(args, timeout=5)

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_GITHUB_TOKEN": "ghp_timeout_secret"},
        run_subprocess=_run,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_AUTH_TIMEOUT"
    assert "ghp_timeout_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_github_runner_exception_is_redacted(tmp_path: Path) -> None:
    def _run(_args: list[str], **_kwargs: object) -> Any:
        raise RuntimeError("transport failed for ghp_exception_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_GITHUB_TOKEN": "ghp_exception_secret"},
        run_subprocess=_run,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_AUTH_UNUSABLE"
    serialized = json.dumps(payload, sort_keys=True)
    assert "ghp_exception_secret" not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_provider_readiness_claude_env_present(tmp_path: Path) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"ANTHROPIC_API_KEY": "anthropic_secret"},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["ok"] is True
    assert claude["reason"] == "CLAUDE_ENV_AUTH_PRESENT"
    assert claude["signals"] == ["ANTHROPIC_API_KEY"]
    assert "anthropic_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_claude_file_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"token":"claude_file_secret"}')

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["ok"] is True
    assert claude["reason"] == "CLAUDE_FILE_AUTH_PRESENT"
    assert "claude_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_gemini_file_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "oauth_creds.json").write_text("gemini_file_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    gemini = payload["providers"]["gemini"]
    assert gemini["ok"] is True
    assert gemini["reason"] == "GEMINI_FILE_AUTH_PRESENT"
    assert "gemini_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_gemini_google_application_credentials_visible(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "google.json"
    credentials.write_text("google_file_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials)},
        run_subprocess=_unexpected_subprocess,
    )

    gemini = payload["providers"]["gemini"]
    assert gemini["ok"] is True
    assert gemini["reason"] == "GEMINI_ENV_AUTH_PRESENT"
    assert gemini["signals"] == ["GOOGLE_APPLICATION_CREDENTIALS"]
    assert "google_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_gemini_missing_google_application_credentials_is_actionable(
    tmp_path: Path,
) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "missing.json")},
        run_subprocess=_unexpected_subprocess,
    )

    gemini = payload["providers"]["gemini"]
    assert gemini["ok"] is False
    assert gemini["reason"] == "GEMINI_AUTH_MISSING"
    assert gemini["signals"] == ["GOOGLE_APPLICATION_CREDENTIALS"]
    assert "file is not visible" in gemini["message"]


@pytest.mark.unit
def test_provider_readiness_opencode_ollama_file_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "config.json").write_text("opencode_file_secret")
    (home / ".ollama" / "models").mkdir(parents=True)
    (home / ".ollama" / "config.json").write_text("ollama_file_secret")
    (home / ".ollama" / "models" / "blob").write_text("model_blob_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1"},
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["ok"] is True
    assert opencode["reason"] == "OPENCODE_FILE_AUTH_PRESENT"
    serialized = json.dumps(payload, sort_keys=True)
    assert "opencode_file_secret" not in serialized
    assert "ollama_file_secret" not in serialized
    assert "model_blob_secret" not in serialized


@pytest.mark.unit
def test_provider_readiness_opencode_ollama_unreachable_fails_when_strict(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)

    def _http_get(_url: str, *, timeout: float) -> Any:
        assert timeout > 0
        raise RuntimeError("connection refused")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1"},
        strict_providers={"opencode"},
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["status"] == "fail"
    assert opencode["reason"] == "OLLAMA_HOST_UNREACHABLE"
    assert "connection refused" in opencode["detail"]


@pytest.mark.unit
def test_provider_readiness_opencode_ollama_file_reason_without_opencode_config(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".ollama").mkdir(parents=True)
    (home / ".ollama" / "id_ed25519").write_text("ollama_private_key_secret")
    urls: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        urls.append(url)
        return SimpleNamespace(status_code=200, text="ok")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"OLLAMA_HOST": "ollama.local:11434"},
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["ok"] is True
    assert opencode["reason"] == "OLLAMA_FILE_AUTH_PRESENT"
    assert urls == ["http://ollama.local:11434/api/version"]
    assert "ollama_private_key_secret" not in json.dumps(payload, sort_keys=True)


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

    monkeypatch.setattr(provider_readiness.subprocess, "run", _subprocess_run)
    monkeypatch.setattr(provider_readiness.httpx, "get", _httpx_get)

    assert provider_readiness._run_subprocess(
        ["gh", "auth", "status"],
        check=False,
        capture_output=True,
        text=True,
        timeout=1.5,
        env={},
    ) is completed
    assert calls == [(["gh", "auth", "status"], 1.5)]
    assert provider_readiness._http_get("http://example.test/api/version", timeout=1.5).text == "ok"


@pytest.mark.unit
def test_provider_readiness_redacts_secret_values_from_details(tmp_path: Path) -> None:
    github_secret = "ghp_super_secret"
    env = {
        "AWF_GITHUB_TOKEN": github_secret,
        "ANTHROPIC_API_KEY": "anthropic_secret",
        "GEMINI_API_KEY": "gemini_secret",
    }

    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args == ["gh", "auth", "status", "--hostname", "github.com"]
        return _completed(
            returncode=1,
            stderr=(
                "failed cloning https://user:ghp_super_secret@github.com/org/repo "
                "with bearer anthropic_secret and gemini_secret"
            ),
        )

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ=env,
        run_subprocess=_run,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_AUTH_UNUSABLE"
    serialized = json.dumps(payload, sort_keys=True)
    assert github_secret not in serialized
    assert "anthropic_secret" not in serialized
    assert "gemini_secret" not in serialized
    assert "<redacted>" in serialized
