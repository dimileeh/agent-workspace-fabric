"""Launch-preflight payload and all-green provider readiness checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import awf.service.provider_readiness as provider_readiness
from awf.service.provider_readiness import collect_agent_readiness

from .test_provider_readiness_part_001 import (
    _completed,
    _ollama_ok,
    _settings,
)


@pytest.mark.unit
def test_preflight_payload_filters_sparse_provider_metadata() -> None:
    provider_result = {
        "ok": True,
        "status": "ok",
        "credential_scope": "fallback_scope",
        "credential_sources": [
            "ignored",
            {},
            {"type": "env", "signal": 42, "credential_scope": "static_env_token"},
            {"signal": "VISIBLE_SIGNAL", "isolation": "service_env"},
        ],
        "warnings": [
            {"reason": "STATIC_TOKEN_FALLBACK", "message": "uses env", "severity": "warning"},
            "ignored",
        ],
    }

    payload = provider_readiness._launch_preflight_payload(
        agent="codex",
        provider="codex",
        model="gpt-5.5",
        model_source="default",
        provider_result=provider_result,
        probe={"status": "unavailable"},
        reason_code="PROVIDER_READY",
        message="ready",
        override=False,
        override_reason=None,
        checked_at=provider_readiness.datetime(2026, 5, 3, tzinfo=provider_readiness.UTC),
        secrets=frozenset(),
    )

    assert payload["readiness_status"] == "ready"
    assert payload["probe_status"] == "unavailable"
    assert payload["auth_source"] == "fallback_scope"
    assert payload["credential_sources"] == [
        {"type": "env", "credential_scope": "static_env_token"},
        {"signal": "VISIBLE_SIGNAL", "isolation": "service_env"},
    ]
    assert payload["warnings"] == [
        {"reason": "STATIC_TOKEN_FALLBACK", "message": "uses env", "severity": "warning"}
    ]
    assert provider_readiness._credential_sources({"credential_sources": "bad-shape"}) == []


@pytest.mark.unit
def test_preflight_reason_and_message_report_missing_model() -> None:
    provider_result = {"ok": True, "status": "ok"}
    probe = {"status": "ok"}

    assert (
        provider_readiness._preflight_reason_code(
            provider_result=provider_result,
            probe=probe,
            model=None,
        )
        == "MODEL_NOT_SELECTED"
    )
    assert (
        provider_readiness._preflight_message(
            provider_result=provider_result,
            probe=probe,
            model=None,
        )
        == "No effective model was selected for the workspace agent."
    )


@pytest.mark.unit
def test_provider_readiness_preflight_snapshot_and_text_redaction(tmp_path: Path) -> None:
    snapshot = {"provider": "codex", "reason_code": "PROVIDER_READY"}

    assert (
        provider_readiness.provider_readiness_preflight_from_task_policy(
            {"provider_readiness_preflight": snapshot}
        )
        == snapshot
    )
    assert (
        provider_readiness.provider_readiness_preflight_from_task_policy(
            {"provider_readiness_preflight": "bad-shape"}
        )
        is None
    )
    redacted = provider_readiness.redact_launch_preflight_text(
        _settings(tmp_path),
        "token sk-proj-redact-text-secret",
        environ={"OPENAI_API_KEY": "sk-proj-redact-text-secret"},
    )
    assert redacted == "token <redacted>"


@pytest.mark.unit
def test_preflight_payload_records_redacted_override_reason_parts() -> None:
    payload = provider_readiness._launch_preflight_payload(
        agent="codex",
        provider="codex",
        model="gpt-5.5",
        model_source="default",
        provider_result={"ok": False, "status": "fail", "reason": "CODEX_AUTH_MISSING"},
        probe={"status": "skipped"},
        reason_code="CODEX_AUTH_MISSING",
        message="missing",
        override=True,
        override_reason="operator checked sk-proj-override-secret manually",
        checked_at=provider_readiness.datetime(2026, 5, 4, tzinfo=provider_readiness.UTC),
        secrets=frozenset({"sk-proj-override-secret"}),
    )

    assert payload["override_reason"] == "operator checked <redacted> manually"
    assert payload["override_reason_redaction_parts"] == [
        "operator checked ",
        " manually",
    ]


@pytest.mark.unit
def test_provider_readiness_all_green(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token":"codex_file_secret"}')
    (home / ".codex" / "config.toml").write_text("model = 'gpt-5.5'\n")
    (home / ".codex" / "installation_id").write_text("installation-123\n")
    (home / ".claude").mkdir(parents=True)
    (home / ".gemini").mkdir()
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".ollama").mkdir()
    (home / ".ollama" / "config.json").write_text("ollama-file-secret")
    github_secret = "ghp_green_secret"
    anthropic_secret = "sk-ant-green-secret"
    cursor_secret = "cursor_green_secret"
    gemini_secret = "gemini_green_secret"
    ollama_secret = "ollama_green_secret"
    xai_secret = "xai_green_secret"
    env = {
        "AWF_GITHUB_TOKEN": github_secret,
        "ANTHROPIC_API_KEY": anthropic_secret,
        "CURSOR_API_KEY": cursor_secret,
        "GEMINI_API_KEY": gemini_secret,
        "OLLAMA_API_KEY": ollama_secret,
        "XAI_API_KEY": xai_secret,
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
    }
    subprocess_calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        """Return successful auth and runtime probes for all providers."""
        subprocess_calls.append(args)
        if args == ["gh", "auth", "status", "--hostname", "github.com"]:
            assert github_secret not in args
            subprocess_env = kwargs["env"]
            assert isinstance(subprocess_env, dict)
            assert subprocess_env["GH_TOKEN"] == github_secret
            return _completed(stdout="logged in\n")
        assert args == [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v cursor-agent",
        ]
        assert kwargs["env"]["CURSOR_API_KEY"] == cursor_secret
        return _completed(stdout="/usr/local/bin/cursor-agent\n")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ=env,
        run_subprocess=_run,
        http_get=_ollama_ok,
    )

    assert payload["status"] == "ok"
    providers = payload["providers"]
    assert set(providers) == {
        "github",
        "codex",
        "claude_code",
        "cursor",
        "gemini",
        "opencode",
        "grok",
        "docker",
    }
    assert all(provider["ok"] is True for provider in providers.values())
    assert providers["github"]["capabilities"] == ["pr_create", "comment", "merge"]
    assert subprocess_calls == [
        ["gh", "auth", "status", "--hostname", "github.com"],
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v cursor-agent",
        ],
    ]
    serialized = json.dumps(payload, sort_keys=True)
    for secret in (
        github_secret,
        "codex_file_secret",
        anthropic_secret,
        cursor_secret,
        gemini_secret,
        ollama_secret,
        xai_secret,
    ):
        assert secret not in serialized
