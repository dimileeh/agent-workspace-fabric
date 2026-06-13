"""Provider credential readiness checks for local service mode."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import awf.service.provider_readiness as provider_readiness
import awf.service.provider_readiness_helpers as provider_readiness_helpers
import awf.service.provider_readiness_provider_checks as provider_readiness_provider_checks
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
    monkeypatch.setattr(
        provider_readiness_provider_checks, "claude_auth_isolation_label", lambda **_: label
    )

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
        provider_readiness_provider_checks,
        "claude_auth_isolation_label",
        lambda **_: "per_workspace_overlay",
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
        provider_readiness_provider_checks,
        "claude_auth_isolation_label",
        lambda **_: "per_workspace_overlay",
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
    # ``claude_auth_isolation_label`` resolves ``_overlay_filesystem_available`` in
    # the ``auth_mounts_claude`` namespace that defines it, so patch it there.
    import awf.node.auth_mounts_claude as auth_mounts_claude

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"token":"claude_file_secret"}')
    monkeypatch.setattr(auth_mounts_claude, "_overlay_filesystem_available", lambda: True)
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
@pytest.mark.parametrize("reserved_char", [",", ":"])
def test_provider_readiness_claude_reports_copy_for_reserved_char_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reserved_char: str
) -> None:
    """A ``,``/``:`` in the work dir forces the copy fallback for every mount.

    ``_prepare_claude_overlay_mount`` degrades to the per-workspace copy when the
    overlay auth root carries a char overlayfs's unescapable ``-o`` payload cannot
    encode. The readiness label must fold in that deterministic host-level signal —
    just like force-copy — so it reports ``per_workspace_copy`` instead of
    overstating overlay isolation, even while overlayfs stays advertised.
    """
    import awf.node.auth_mounts_claude as auth_mounts_claude

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"token":"claude_file_secret"}')
    # Overlayfs is advertised and no force-copy request is in play; only the
    # reserved char in the work dir should flip the posture to copy.
    monkeypatch.setattr(auth_mounts_claude, "_overlay_filesystem_available", lambda: True)

    work_dir = tmp_path / f"work{reserved_char}root"

    payload = collect_agent_readiness(
        _settings(tmp_path, host_home=str(home), work_dir=str(work_dir)),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["ok"] is True
    assert claude["reason"] == "CLAUDE_FILE_AUTH_PRESENT"
    assert claude["isolation"] == "per_workspace_copy"
    assert all(
        source["isolation"] == "per_workspace_copy" for source in claude["credential_sources"]
    )


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
        raise httpx.ConnectError("connection refused")

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
            raise httpx.ConnectError("nodename nor servname provided")
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
            raise httpx.ConnectError("transport failed for sk-proj-ollama-fallback-secret")
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
                    "detail": "ConnectError: transport failed for <redacted>",
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
            raise httpx.ConnectError("transport failed for sk-proj-ollama-terminal-secret")
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
    assert "ConnectError: transport failed for <redacted>" in result["detail"]
    assert "HTTP 503: busy <redacted>" in result["detail"]
    messages = [record.getMessage() for record in caplog.records]
    traceback_messages = [message for message in messages if "Traceback" in message]
    terminal_http_messages = [message for message in messages if "HTTP 503: busy" in message]
    assert len(traceback_messages) == 1
    assert len(terminal_http_messages) == 1
    assert "ConnectError: transport failed for <redacted>" in traceback_messages[0]
    assert "ConnectError: transport failed" not in terminal_http_messages[0]
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
            raise httpx.ConnectError("transport failed for sk-proj-ollama-terminal-secret")
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
    assert "ConnectError: transport failed for <redacted>" in caplog.text
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
@pytest.mark.parametrize(
    "env",
    [
        # Unbalanced IPv6 brackets make ``urlsplit`` raise ``ValueError``.
        {"OLLAMA_HOST": "http://[::1"},
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://[bad:11434"},
        # A non-numeric port makes the lazy ``.port`` accessor raise ``ValueError``
        # on a host the worker would otherwise treat as reachable (so the probe is
        # not deferred and the URL builder runs).
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://localhost:notaport"},
    ],
)
def test_ollama_url_helpers_normalize_malformed_base_url_to_default(
    env: dict[str, str],
) -> None:
    """A malformed base URL must not escape as a ``ValueError`` from the probe/pull
    URL builder during readiness. It normalizes to the ``host.docker.internal``
    default like a blank value, matching the worker reachability classifier."""
    assert provider_readiness_helpers._ollama_version_urls(env) == (
        "http://host.docker.internal:11434/api/version",
        "http://localhost:11434/api/version",
    )
    assert provider_readiness_helpers._ollama_tags_urls(env) == (
        "http://host.docker.internal:11434/api/tags",
        "http://localhost:11434/api/tags",
    )


@pytest.mark.unit
def test_overlay_profile_ollama_base_url_resolves_required_placeholder() -> None:
    """A profile may declare the Ollama endpoint via Compose's required form
    (``${OLLAMA_URL:?set OLLAMA_URL}``). When the variable is present in the worker
    environ the overlay resolves it like any other placeholder."""
    result = provider_readiness_helpers.overlay_profile_ollama_base_url(
        {"OLLAMA_URL": "http://ollama-sidecar:11434"},
        {
            "name": "ollama-sidecar",
            "runtime": {
                "environment": {
                    "AWF_OPENCODE_OLLAMA_BASE_URL": "${OLLAMA_URL:?set OLLAMA_URL}",
                }
            },
        },
    )

    assert result["AWF_OPENCODE_OLLAMA_BASE_URL"] == "http://ollama-sidecar:11434"


@pytest.mark.unit
def test_overlay_profile_ollama_base_url_treats_unset_required_placeholder_as_undeclared() -> None:
    """The required form raises ``ComposeEnvInterpolationError`` when the variable is
    absent. This overlay runs during create/retry admission before provider readiness
    — paths that only translate profile/readiness exceptions — so an unhandled raise
    would escape as a 500. The worker environ is a best-effort approximation of the
    agent's Compose context, so an unresolvable required placeholder is treated as
    undeclared (the supplied environ is returned unchanged), not surfaced as an error."""
    environ = {"OLLAMA_HOST": "http://worker-daemon:11434"}

    result = provider_readiness_helpers.overlay_profile_ollama_base_url(
        environ,
        {
            "name": "ollama-required",
            "runtime": {
                "environment": {
                    "AWF_OPENCODE_OLLAMA_BASE_URL": "${OLLAMA_URL:?set OLLAMA_URL}",
                }
            },
        },
    )

    assert result == environ
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in result


@pytest.mark.unit
def test_overlay_profile_provider_credentials_overlays_profile_declared_key() -> None:
    """A provider API key declared solely in the profile's runtime.environment reaches
    the agent container but not the worker process the non-Ollama credential gate runs
    in. The overlay brings the profile-declared key into the readiness environ so a
    workspace is not blocked with OPENCODE_PROVIDER_AUTH_MISSING for a credential the
    agent would use — symmetric to overlay_profile_ollama_base_url."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {},
        {
            "name": "opencode-openai",
            "runtime": {"environment": {"OPENAI_API_KEY": "sk-proj-profile-only"}},
        },
    )

    assert result["OPENAI_API_KEY"] == "sk-proj-profile-only"


@pytest.mark.unit
def test_overlay_profile_provider_credentials_overlays_ollama_cloud_key() -> None:
    """An ``OLLAMA_API_KEY`` declared solely in the profile env reaches the agent
    container but not the worker. A ``:cloud`` Ollama model whose sidecar base URL is
    unreachable from the worker needs that credential visible for the host-probe defer
    path; without the overlay the credential gate would falsely block the workspace with
    OPENCODE_OLLAMA_AUTH_MISSING. The overlay brings the profile-declared Ollama Cloud
    key into the readiness environ alongside the non-Ollama provider keys."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {},
        {
            "name": "opencode-ollama-cloud",
            "runtime": {
                "environment": {
                    "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434",
                    "OLLAMA_API_KEY": "ollama-cloud-profile-only",
                },
            },
        },
    )

    assert result["OLLAMA_API_KEY"] == "ollama-cloud-profile-only"


@pytest.mark.unit
def test_overlay_profile_provider_credentials_resolves_placeholder_against_environ() -> None:
    """Profile env values may carry Compose-style ``${NAME}`` placeholders resolved by
    the agent container. The worker-side overlay resolves them against ``environ``; an
    unresolvable required placeholder is treated as undeclared, not surfaced as a 500."""
    declared = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_ANTHROPIC_KEY": "sk-ant-host"},
        {
            "name": "opencode-anthropic",
            "runtime": {"environment": {"ANTHROPIC_API_KEY": "${HOST_ANTHROPIC_KEY}"}},
        },
    )
    assert declared["ANTHROPIC_API_KEY"] == "sk-ant-host"

    undeclared = provider_readiness_helpers.overlay_profile_provider_credentials(
        {},
        {
            "name": "opencode-anthropic",
            "runtime": {"environment": {"ANTHROPIC_API_KEY": "${MISSING_KEY:?set MISSING_KEY}"}},
        },
    )
    assert "ANTHROPIC_API_KEY" not in undeclared

    # A plain ``${NAME}`` placeholder resolving to empty is also treated as undeclared.
    empty = provider_readiness_helpers.overlay_profile_provider_credentials(
        {},
        {
            "name": "opencode-anthropic",
            "runtime": {"environment": {"ANTHROPIC_API_KEY": "${MISSING_KEY}"}},
        },
    )
    assert "ANTHROPIC_API_KEY" not in empty


@pytest.mark.unit
def test_overlay_profile_provider_credentials_no_profile_returns_environ_unchanged() -> None:
    """A workspace without a resolved profile (or an unvalidatable snapshot) falls back
    to the supplied environ unchanged."""
    environ = {"OPENAI_API_KEY": "sk-proj-worker"}

    assert provider_readiness_helpers.overlay_profile_provider_credentials(environ, None) == (
        environ
    )
    assert (
        provider_readiness_helpers.overlay_profile_provider_credentials(
            environ, {"not": "a valid profile snapshot"}
        )
        == environ
    )


@pytest.mark.unit
def test_overlay_profile_provider_credentials_overlays_env_secret_lease() -> None:
    """A provider key supplied through a profile ``kind="env"`` secret lease (target
    ``OPENAI_API_KEY``, ``ref="env/HOST_OPENAI_KEY"``) reaches the agent via the launcher's
    secret-lease environment merge, not ``runtime.environment``. The overlay must resolve
    the lease's host source against ``environ`` too, so admission does not block such a
    workspace with OPENCODE_PROVIDER_AUTH_MISSING before the lease is resolved."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-lease",
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert result["OPENAI_API_KEY"] == "sk-proj-host-lease"


@pytest.mark.unit
def test_overlay_profile_provider_credentials_overlays_ollama_cloud_env_lease() -> None:
    """``OLLAMA_API_KEY`` supplied through a ``kind="env"`` secret lease is overlaid for the
    same reason as the non-Ollama provider keys: a ``:cloud`` Ollama model whose sidecar is
    unreachable from the worker needs the credential visible for the host-probe defer path."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OLLAMA_KEY": "ollama-cloud-host-lease"},
        {
            "name": "opencode-ollama-cloud-lease",
            "secrets": [
                {
                    "name": "ollama-key",
                    "kind": "env",
                    "target": "OLLAMA_API_KEY",
                    "ref": "env/HOST_OLLAMA_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert result["OLLAMA_API_KEY"] == "ollama-cloud-host-lease"


@pytest.mark.unit
def test_overlay_profile_provider_credentials_env_lease_runtime_wins() -> None:
    """``runtime.environment`` is first-writer-wins over secret leases in the agent env
    (``merge_agent_environment``), so when both declare the same target the overlay keeps
    the runtime value rather than the lease's host source."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-both",
            "runtime": {"environment": {"OPENAI_API_KEY": "sk-proj-runtime"}},
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert result["OPENAI_API_KEY"] == "sk-proj-runtime"


@pytest.mark.unit
def test_overlay_profile_provider_credentials_env_lease_unresolvable_runtime_wins() -> None:
    """``runtime.environment`` owns the key even when its value cannot be resolved here.

    When ``runtime.environment`` declares the provider key with an unset required
    placeholder (``${MISSING:?set}``) — or one resolving empty — the launcher's
    first-writer-wins merge keeps the failing runtime placeholder and drops the lease, so
    the agent never receives the lease's host credential. The overlay must not surface the
    lease's value, otherwise create/retry preflight would admit a workspace with credentials
    the agent will not actually receive."""
    required = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-required-placeholder",
            "runtime": {"environment": {"OPENAI_API_KEY": "${MISSING_KEY:?set MISSING_KEY}"}},
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert "OPENAI_API_KEY" not in required

    empty = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-empty-placeholder",
            "runtime": {"environment": {"OPENAI_API_KEY": "${MISSING_KEY}"}},
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert "OPENAI_API_KEY" not in empty


@pytest.mark.unit
def test_overlay_profile_provider_credentials_env_lease_missing_source_undeclared() -> None:
    """An env secret lease whose host source is absent (or empty) from ``environ`` is treated
    as undeclared — the launcher omits an optional lease and fails a required one, but the
    best-effort admission overlay simply does not surface a credential it cannot resolve.
    A non-provider lease target is ignored, and a non-``env`` lease kind is left alone."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {},
        {
            "name": "opencode-openai-missing",
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                },
                {
                    "name": "unrelated",
                    "kind": "env",
                    "target": "SOME_OTHER_VAR",
                    "ref": "env/HOST_OTHER",
                    "provider": "env",
                },
                {
                    "name": "mounted-key",
                    "kind": "mount",
                    "target": "/run/secrets/openai",
                    "ref": "local-file:/host/openai",
                    "provider": "local-file",
                },
            ],
        },
    )

    assert "OPENAI_API_KEY" not in result
