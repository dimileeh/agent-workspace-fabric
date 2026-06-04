"""Provider credential readiness checks for local service mode."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    if url == "http://ollama.local:11434/api/version":
        return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
    if url == "http://ollama.local:11434/api/tags":
        return SimpleNamespace(
            status_code=200,
            text='{"models":[{"name":"kimi-k2.6:cloud"}]}',
        )
    raise AssertionError(f"unexpected Ollama probe URL: {url}")


def _runtime_cli_ok(expected_executable: str) -> Any:
    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args == [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            f"command -v {expected_executable}",
        ]
        return _completed(stdout=f"/usr/bin/{expected_executable}\n")

    return _run


@pytest.mark.unit
def test_provider_readiness_primary_scope_and_isolation_fallbacks() -> None:
    assert (
        provider_readiness._primary_credential_scope([{"credential_scope": "unknown"}])
        == "not_observed"
    )
    assert provider_readiness._primary_isolation([{"isolation": "unknown"}]) == "none"


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
def test_provider_readiness_github_runner_exception_is_redacted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)

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
    assert "provider_readiness.github_auth_check_exception" in caplog.text
    assert "RuntimeError: transport failed for <redacted>" in caplog.text
    assert "Traceback" in caplog.text
    assert "ghp_exception_secret" not in caplog.text


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
@pytest.mark.parametrize("label", ["per_workspace_overlay", "per_workspace_copy"])
def test_provider_readiness_claude_file_reports_overlay_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"token":"claude_file_secret"}')
    monkeypatch.setattr(provider_readiness, "claude_auth_isolation_label", lambda **_: label)

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["ok"] is True
    assert claude["reason"] == "CLAUDE_FILE_AUTH_PRESENT"
    assert claude["credential_scope"] == "isolated_workspace"
    assert claude["isolation"] == label
    assert all(source["isolation"] == label for source in claude["credential_sources"])


@pytest.mark.unit
def test_provider_readiness_claude_json_only_reports_copy_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.claude.json``-only hosts must not claim overlay isolation.

    The resolver always copies ``~/.claude.json`` per workspace (never overlays
    it), so even when overlayfs is available the file source — and the provider
    posture when it is the only credential source — stays ``per_workspace_copy``.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"token":"claude_file_secret"}')
    monkeypatch.setattr(
        provider_readiness, "claude_auth_isolation_label", lambda **_: "per_workspace_overlay"
    )

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["ok"] is True
    assert claude["reason"] == "CLAUDE_FILE_AUTH_PRESENT"
    assert claude["isolation"] == "per_workspace_copy"
    assert claude["credential_sources"] == [
        {
            "type": "path",
            "signal": "~/.claude.json",
            "credential_scope": "isolated_workspace",
            "isolation": "per_workspace_copy",
        }
    ]
    assert "claude_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_claude_dir_keeps_overlay_json_stays_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a ``~/.claude`` dir present the dir keeps overlay; the file stays copy."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"token":"claude_dir_secret"}')
    (home / ".claude.json").write_text('{"token":"claude_json_secret"}')
    monkeypatch.setattr(
        provider_readiness, "claude_auth_isolation_label", lambda **_: "per_workspace_overlay"
    )

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["isolation"] == "per_workspace_overlay"
    isolation_by_signal = {
        source["signal"]: source["isolation"] for source in claude["credential_sources"]
    }
    assert isolation_by_signal == {
        "~/.claude": "per_workspace_overlay",
        "~/.claude.json": "per_workspace_copy",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("force_copy_env", "expected"),
    [
        ("true", "per_workspace_copy"),
        ("", "per_workspace_overlay"),
    ],
)
def test_provider_readiness_claude_honors_preflighted_force_copy_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_copy_env: str,
    expected: str,
) -> None:
    """The force-copy probe reads the passed ``environ``, not ``os.environ``.

    ``awf service bootstrap`` on a non-propagating host folds
    ``AWF_CLAUDE_AUTH_FORCE_COPY=true`` into the readiness ``environ`` dict rather
    than the CLI process environment, so readiness must read that dict or it would
    report ``per_workspace_overlay`` while the worker uses the copy fallback. Here
    overlayfs is advertised as available, so the label only flips to copy when the
    passed env requests it.
    """
    import awf.node.auth_mounts as auth_mounts

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"token":"claude_file_secret"}')
    monkeypatch.setattr(auth_mounts, "_overlay_filesystem_available", lambda: True)
    # The process environment must NOT carry the request; only the passed env does.
    monkeypatch.delenv("AWF_CLAUDE_AUTH_FORCE_COPY", raising=False)

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_CLAUDE_AUTH_FORCE_COPY": force_copy_env},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["ok"] is True
    assert claude["reason"] == "CLAUDE_FILE_AUTH_PRESENT"
    assert claude["isolation"] == expected
    assert all(source["isolation"] == expected for source in claude["credential_sources"])


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
def test_provider_readiness_grok_file_present_before_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "auth.json").write_text('{"token":"grok_file_secret"}')

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"XAI_API_KEY": "xai_env_secret"},
        run_subprocess=_runtime_cli_ok("grok"),
    )

    grok = payload["providers"]["grok"]
    assert grok["ok"] is True
    assert grok["reason"] == "GROK_FILE_AUTH_PRESENT"
    assert grok["signals"] == ["~/.grok/auth.json"]
    assert grok["credential_scope"] == "isolated_workspace"
    assert grok["isolation"] == "per_workspace_copy"
    assert grok["warnings"] == []
    serialized = json.dumps(payload, sort_keys=True)
    assert "grok_file_secret" not in serialized
    assert "xai_env_secret" not in serialized


@pytest.mark.unit
def test_provider_readiness_grok_ignores_non_file_auth_json(tmp_path: Path) -> None:
    """Non-regular-file at ~/.grok/auth.json must not mark file auth present.

    _check_grok must use is_file (to match _prepare_isolated_grok_auth) so a
    directory, symlink-to-dir, or other non-file does not report
    GROK_FILE_AUTH_PRESENT. Otherwise preflight can pass while no .grok mount
    is created and XAI_API_KEY env fallback may be skipped by the caller.
    Regression test for GitHub PR review thread PRRT_kwDOSJAM6s6G0PEp.
    """
    home = tmp_path / "home"
    grok_dir = home / ".grok"
    grok_dir.mkdir(parents=True)
    # Exists but not a regular file (a dir with the auth.json name).
    (grok_dir / "auth.json").mkdir()

    # With XAI env present: must prefer env path, not claim file auth.
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"XAI_API_KEY": "xai_env_secret"},
        run_subprocess=_runtime_cli_ok("grok"),
    )

    grok = payload["providers"]["grok"]
    assert grok["ok"] is True
    assert grok["reason"] == "GROK_ENV_AUTH_PRESENT"
    assert grok["signals"] == ["XAI_API_KEY"]
    assert grok["credential_scope"] == "static_env_token"
    assert grok["isolation"] == "service_env"
    assert grok["warnings"] != []
    serialized = json.dumps(payload, sort_keys=True)
    assert "xai_env_secret" not in serialized

    # No env: must report missing (never file present from non-file path).
    payload2 = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )
    grok2 = payload2["providers"]["grok"]
    assert grok2["ok"] is False
    assert grok2["reason"] == "GROK_AUTH_MISSING"


@pytest.mark.unit
def test_provider_readiness_cursor_env_present(tmp_path: Path) -> None:
    """Cursor env auth appears as a static service-env token."""
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"CURSOR_API_KEY": "cursor_env_secret"},
        run_subprocess=_runtime_cli_ok("cursor-agent"),
    )

    cursor = payload["providers"]["cursor"]
    assert cursor["ok"] is True
    assert cursor["reason"] == "CURSOR_ENV_AUTH_PRESENT"
    assert cursor["signals"] == ["CURSOR_API_KEY"]
    assert cursor["credential_scope"] == "static_env_token"
    assert cursor["isolation"] == "service_env"
    assert cursor["warnings"] == [
        {
            "reason": "STATIC_TOKEN_FALLBACK",
            "message": (
                "Cursor auth is supplied by static service environment variable CURSOR_API_KEY."
            ),
            "severity": "warning",
        }
    ]
    assert "cursor_env_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_cursor_missing_env_fails_when_strict(tmp_path: Path) -> None:
    """Strict Cursor readiness fails when no Cursor auth signal exists."""
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        strict_providers={"cursor"},
        run_subprocess=_unexpected_subprocess,
    )

    cursor = payload["providers"]["cursor"]
    assert cursor["status"] == "fail"
    assert cursor["reason"] == "CURSOR_AUTH_MISSING"
    assert cursor["credential_scope"] == "not_observed"
    assert cursor["isolation"] == "none"


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
def test_provider_readiness_opencode_ollama_redirect_fails(tmp_path: Path) -> None:
    def _http_get(_url: str, *, timeout: float) -> Any:
        assert timeout > 0
        return SimpleNamespace(status_code=302, text="redirect to login")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={
            "OLLAMA_API_KEY": "ollama_env_secret",
            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
        },
        strict_providers={"opencode"},
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["status"] == "fail"
    assert opencode["reason"] == "OLLAMA_HOST_UNREACHABLE"
    assert opencode["detail"] == "HTTP 302: redirect to login"


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
def test_provider_readiness_opencode_default_host_gateway_falls_back_to_localhost(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    urls: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        urls.append(url)
        if url == "http://host.docker.internal:11434/api/version":
            raise RuntimeError("nodename nor servname provided")
        return SimpleNamespace(status_code=200, text="ok")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["ok"] is True
    assert opencode["reason"] == "OPENCODE_FILE_AUTH_PRESENT"
    assert urls == [
        "http://host.docker.internal:11434/api/version",
        "http://localhost:11434/api/version",
    ]
    serialized = json.dumps(payload, sort_keys=True)
    assert "debug" not in opencode
    assert "provider_readiness.ollama_probe_exception" not in caplog.text
    assert "Traceback" not in caplog.text
    assert "nodename nor servname provided" not in caplog.text
    assert "nodename nor servname provided" not in serialized


@pytest.mark.unit
def test_ollama_http_probe_records_recovered_failures_as_redacted_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)
    urls: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        urls.append(url)
        if url == "http://host.docker.internal:11434/api/version":
            raise RuntimeError("transport failed for sk-proj-ollama-fallback-secret")
        if url == "http://localhost:11434/api/version":
            return SimpleNamespace(status_code=200, text="ok")
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = provider_readiness._probe_ollama(
        (
            "http://host.docker.internal:11434/api/version",
            "http://localhost:11434/api/version",
        ),
        http_get=_http_get,
        secrets=frozenset({"sk-proj-ollama-fallback-secret"}),
    )

    assert result == {
        "ok": True,
        "debug": {
            "recovered_failures": [
                {
                    "url": "http://host.docker.internal:11434/api/version",
                    "status": "exception",
                    "detail": "RuntimeError: transport failed for <redacted>",
                }
            ]
        },
    }
    assert urls == [
        "http://host.docker.internal:11434/api/version",
        "http://localhost:11434/api/version",
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert "provider_readiness.ollama_probe_exception" not in caplog.text
    assert "Traceback" not in caplog.text
    assert "sk-proj-ollama-fallback-secret" not in serialized
    assert "sk-proj-ollama-fallback-secret" not in caplog.text


@pytest.mark.unit
def test_ollama_http_probe_records_recovered_http_failure_as_redacted_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)
    urls: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        urls.append(url)
        if url == "http://host.docker.internal:11434/api/version":
            return SimpleNamespace(
                status_code=401,
                text="unauthorized for sk-proj-ollama-fallback-secret",
            )
        if url == "http://localhost:11434/api/version":
            return SimpleNamespace(status_code=200, text="ok")
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = provider_readiness._probe_ollama(
        (
            "http://host.docker.internal:11434/api/version",
            "http://localhost:11434/api/version",
        ),
        http_get=_http_get,
        secrets=frozenset({"sk-proj-ollama-fallback-secret"}),
    )

    assert result == {
        "ok": True,
        "debug": {
            "recovered_failures": [
                {
                    "url": "http://host.docker.internal:11434/api/version",
                    "status": "http_error",
                    "detail": "HTTP 401: unauthorized for <redacted>",
                    "status_code": 401,
                }
            ]
        },
    }
    assert urls == [
        "http://host.docker.internal:11434/api/version",
        "http://localhost:11434/api/version",
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert "provider_readiness.ollama_probe_exception" not in caplog.text
    assert "Traceback" not in caplog.text
    assert "sk-proj-ollama-fallback-secret" not in serialized
    assert "sk-proj-ollama-fallback-secret" not in caplog.text


@pytest.mark.unit
def test_ollama_probe_failure_debug_redacts_before_truncating_long_detail() -> None:
    secret = "sk-proj-ollama-boundary-secret"
    detail = ("x" * 225) + secret + "-tail"

    result = provider_readiness_helpers._ollama_probe_failure_debug(
        url="http://ollama.local/api/version",
        status="http_error",
        detail=detail,
        secrets=frozenset({secret}),
    )

    serialized = json.dumps(result, sort_keys=True)
    assert secret not in serialized
    assert "sk-proj-ollama-boundary" not in serialized
    assert "<redacted>" in result["detail"]


@pytest.mark.unit
def test_ollama_http_probe_terminal_mixed_failure_logs_only_http_terminal_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://host.docker.internal:11434/api/version":
            raise RuntimeError("transport failed for sk-proj-ollama-terminal-secret")
        if url == "http://localhost:11434/api/version":
            return SimpleNamespace(status_code=503, text="busy ghp_ollama_terminal_secret")
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = provider_readiness._probe_ollama(
        (
            "http://host.docker.internal:11434/api/version",
            "http://localhost:11434/api/version",
        ),
        http_get=_http_get,
        secrets=frozenset(
            {
                "sk-proj-ollama-terminal-secret",
                "ghp_ollama_terminal_secret",
            }
        ),
    )

    assert result["ok"] is False
    assert "RuntimeError: transport failed for <redacted>" in result["detail"]
    assert "HTTP 503: busy <redacted>" in result["detail"]
    messages = [record.getMessage() for record in caplog.records]
    traceback_messages = [message for message in messages if "Traceback" in message]
    terminal_http_messages = [message for message in messages if "HTTP 503: busy" in message]
    assert len(traceback_messages) == 1
    assert len(terminal_http_messages) == 1
    assert "RuntimeError: transport failed for <redacted>" in traceback_messages[0]
    assert "RuntimeError: transport failed" not in terminal_http_messages[0]
    assert "HTTP 503: busy <redacted>" in terminal_http_messages[0]
    assert "sk-proj-ollama-terminal-secret" not in caplog.text
    assert "ghp_ollama_terminal_secret" not in caplog.text


@pytest.mark.unit
def test_provider_readiness_opencode_all_ollama_candidates_fail_reports_redacted_detail(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    urls: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        urls.append(url)
        if url == "http://host.docker.internal:11434/api/version":
            raise RuntimeError("transport failed for sk-proj-ollama-terminal-secret")
        if url == "http://localhost:11434/api/version":
            return SimpleNamespace(status_code=503, text="busy ghp_ollama_terminal_secret")
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        strict_providers={"opencode"},
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert payload["status"] == "fail"
    assert opencode["status"] == "fail"
    assert opencode["reason"] == "OLLAMA_HOST_UNREACHABLE"
    assert urls == [
        "http://host.docker.internal:11434/api/version",
        "http://localhost:11434/api/version",
    ]
    assert "http://host.docker.internal:11434/api/version" in opencode["detail"]
    assert "http://localhost:11434/api/version" in opencode["detail"]
    assert "<redacted>" in opencode["detail"]
    serialized = json.dumps(payload, sort_keys=True)
    assert "sk-proj-ollama-terminal-secret" not in serialized
    assert "ghp_ollama_terminal_secret" not in serialized
    assert "provider_readiness.ollama_probe_exception" in caplog.text
    assert "Traceback" in caplog.text
    assert "RuntimeError: transport failed for <redacted>" in caplog.text
    assert "HTTP 503: busy <redacted>" in caplog.text
    assert "sk-proj-ollama-terminal-secret" not in caplog.text
    assert "ghp_ollama_terminal_secret" not in caplog.text


@pytest.mark.unit
def test_ollama_url_helpers_normalize_v1_and_host_gateway() -> None:
    env = {"OLLAMA_HOST": "host.docker.internal:11434/v1"}

    assert provider_readiness_helpers._ollama_version_url(env) == (
        "http://host.docker.internal:11434/api/version"
    )
    assert provider_readiness._ollama_tags_urls(env) == (
        "http://host.docker.internal:11434/api/tags",
        "http://localhost:11434/api/tags",
    )


@pytest.mark.unit
def test_ollama_url_helpers_preserve_host_gateway_fallback_port() -> None:
    env = {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://host.docker.internal:23456/v1"}

    assert provider_readiness_helpers._ollama_version_urls(env) == (
        "http://host.docker.internal:23456/api/version",
        "http://localhost:23456/api/version",
    )


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
        raise RuntimeError("transport failed for sk-proj-ollama-secret")

    result = provider_readiness._probe_ollama(
        ("http://ollama.local:11434/api/version",),
        http_get=_http_get,
        secrets=frozenset({"sk-proj-ollama-secret"}),
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["ok"] is False
    assert "RuntimeError: transport failed for <redacted>" in serialized
    assert "provider_readiness.ollama_probe_exception" in caplog.text
    assert "Traceback" in caplog.text
    assert "RuntimeError: transport failed for <redacted>" in caplog.text
    assert "sk-proj-ollama-secret" not in serialized
    assert "sk-proj-ollama-secret" not in caplog.text


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
            raise RuntimeError("connect failed")
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
            raise RuntimeError("connect failed for sk-proj-ollama-secret")
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
                    "detail": "RuntimeError: connect failed for <redacted>",
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
            raise RuntimeError("connect failed for sk-proj-ollama-secret")
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
        "probe_failures=http://primary.local/api/tags: RuntimeError: connect failed for <redacted>"
        in result["detail"]
    )
    assert "provider_readiness.ollama_model_probe_exception" in caplog.text
    assert "Traceback" in caplog.text
    assert "RuntimeError: connect failed for <redacted>" in caplog.text
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
