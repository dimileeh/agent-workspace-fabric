"""CLI coverage for ``awf init`` forge detection + forge-scoped auth (issue #539).

Forge detection (GitHub vs Bitbucket) and any ``gh`` requirement moved from the
repo-agnostic host/service readiness layer to ``awf init <PATH>`` project
onboarding, where the repo's ``git remote`` is in scope. These tests pin the
detection branches (github / bitbucket / no-remote / explicit-override /
unknown-host / present-but-unauthenticated) and the structured JSON contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


def _stub_prereqs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub local-service collection so onboarding reaches forge detection."""

    async def _collect_service_status(_settings: object, **_kwargs: object) -> dict[str, object]:
        return {
            "service": "awf",
            "status": "ok",
            "checks": {},
            "agent_readiness": {"status": "ok"},
        }

    async def _collect_doctor_report(_settings: object, **_kwargs: object) -> object:
        return SimpleNamespace(status="ok", diagnostics=())

    # A non-None settings object so the github branch exercises readiness; the
    # readiness probe itself is stubbed per-test so it never shells out.
    monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: object())
    monkeypatch.setattr("awf.service.config.local_service_environ", lambda *_a, **_k: {})
    monkeypatch.setattr("awf.service.status.collect_service_status", _collect_service_status)
    monkeypatch.setattr("awf.service.doctor.collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(
        "awf.service.doctor.render_doctor_pretty",
        lambda report: f"AWF doctor: {getattr(report, 'status', 'unknown')}\n",
    )


def _stub_origin(monkeypatch: pytest.MonkeyPatch, repo_url: str | None) -> None:
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: repo_url,
    )


def _gh_result(level_ok: bool) -> Any:
    from awf.host_setup.system_checks import SetupCheckLevel, SetupCheckResult

    return SetupCheckResult(
        name="gh",
        level=SetupCheckLevel.OK if level_ok else SetupCheckLevel.WARNING,
        summary="ok" if level_ok else "missing",
        detail="ok",
    )


def _stub_check_gh(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    monkeypatch.setattr(
        "awf.host_setup.system_checks.check_gh",
        lambda **_kwargs: _gh_result(present),
    )


def _forbid_check_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove the bitbucket path never touches the GitHub CLI presence probe."""

    def _boom(**_kwargs: object) -> Any:
        raise AssertionError("check_gh must not run for a bitbucket repo")

    monkeypatch.setattr("awf.host_setup.system_checks.check_gh", _boom)


def _stub_github_readiness(monkeypatch: pytest.MonkeyPatch, *, ok: bool) -> None:
    monkeypatch.setattr(
        "awf.service.provider_readiness.check_single_provider_readiness",
        lambda *_a, **_k: {"ok": ok},
    )


@pytest.mark.unit
def test_init_github_repo_runs_gh_and_readiness_and_emits_github_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_prereqs(monkeypatch)
    _stub_origin(monkeypatch, "https://github.com/acme/widgets.git")
    _stub_check_gh(monkeypatch, present=True)
    _stub_github_readiness(monkeypatch, ok=True)

    result = _runner.invoke(app, ["init", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    forge = json.loads(result.output)["forge"]
    assert forge["forge"] == "github"
    assert forge["host_detected"] is True
    assert forge["host"] == "github.com"
    assert forge["auth"]["gh_present"] is True
    assert forge["auth"]["github_readiness"] == "ok"
    assert forge["auth_ok"] is True
    assert forge["level"] == "ok"

    pretty = _runner.invoke(app, ["init", str(tmp_path)])
    assert pretty.exit_code == 0, pretty.output
    assert "Detected forge: github." in pretty.output
    assert "GitHub CLI (gh)" in pretty.output


@pytest.mark.unit
def test_init_github_present_but_unauthenticated_reports_both_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_prereqs(monkeypatch)
    _stub_origin(monkeypatch, "git@github.com:acme/widgets.git")
    _stub_check_gh(monkeypatch, present=True)
    _stub_github_readiness(monkeypatch, ok=False)

    result = _runner.invoke(app, ["init", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output  # WARNING, never blocking
    forge = json.loads(result.output)["forge"]
    assert forge["auth"]["gh_present"] is True
    assert forge["auth"]["github_readiness"] == "fail"
    assert forge["auth_ok"] is False
    assert forge["level"] == "warning"

    pretty = _runner.invoke(app, ["init", str(tmp_path)])
    assert "GitHub auth not verified" in pretty.output
    assert "AWF_GITHUB_TOKEN" in pretty.output


@pytest.mark.unit
def test_init_bitbucket_repo_verifies_trio_and_never_mentions_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_prereqs(monkeypatch)
    _stub_origin(monkeypatch, "git@bitbucket.org:acme/widgets.git")
    _forbid_check_gh(monkeypatch)
    # Partial env: only the token is set, so EMAIL + AUTH_MODE must be named.
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda *_a, **_k: {"BITBUCKET_API_TOKEN": "tok"},
    )

    result = _runner.invoke(app, ["init", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    forge = json.loads(result.output)["forge"]
    assert forge["forge"] == "bitbucket"
    assert forge["auth"]["bitbucket_keys"] == {
        "BITBUCKET_API_TOKEN": True,
        "BITBUCKET_EMAIL": False,
        "BITBUCKET_AUTH_MODE": False,
    }
    assert forge["auth"]["missing"] == ["BITBUCKET_EMAIL", "BITBUCKET_AUTH_MODE"]
    assert forge["auth_ok"] is False

    pretty = _runner.invoke(app, ["init", str(tmp_path)])
    assert pretty.exit_code == 0, pretty.output
    assert "Detected forge: bitbucket." in pretty.output
    assert "BITBUCKET_EMAIL, BITBUCKET_AUTH_MODE" in pretty.output
    # A bitbucket repo must see zero gh noise.
    assert "GitHub" not in pretty.output
    assert "gh auth" not in pretty.output
    assert "install gh" not in pretty.output.lower()


@pytest.mark.unit
def test_init_bitbucket_repo_full_env_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_prereqs(monkeypatch)
    _stub_origin(monkeypatch, "https://bitbucket.org/acme/widgets.git")
    _forbid_check_gh(monkeypatch)
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda *_a, **_k: {
            "BITBUCKET_API_TOKEN": "tok",
            "BITBUCKET_EMAIL": "dev@example.com",
            "BITBUCKET_AUTH_MODE": "basic",
        },
    )

    result = _runner.invoke(app, ["init", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    forge = json.loads(result.output)["forge"]
    assert forge["forge"] == "bitbucket"
    assert forge["auth"]["missing"] == []
    assert forge["auth_ok"] is True
    assert forge["level"] == "ok"

    pretty = _runner.invoke(app, ["init", str(tmp_path)])
    assert "Bitbucket auth env present" in pretty.output


@pytest.mark.unit
def test_init_no_origin_remote_stays_neutral_and_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_prereqs(monkeypatch)
    _stub_origin(monkeypatch, None)
    _forbid_check_gh(monkeypatch)

    result = _runner.invoke(app, ["init", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    forge = json.loads(result.output)["forge"]
    assert forge["forge"] is None
    assert forge["host"] is None
    assert forge["host_detected"] is False
    assert forge["auth"] is None
    assert forge["level"] == "neutral"

    pretty = _runner.invoke(app, ["init", str(tmp_path)])
    assert "No `origin` remote detected" in pretty.output


@pytest.mark.unit
def test_init_explicit_forge_override_wins_over_url_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_prereqs(monkeypatch)
    # Origin host is github.com, but the on-disk workspace profile pins
    # ``forge: bitbucket``. ``awf init`` drafts a fresh profile (always
    # ``forge: auto``) and never reads this file into the preview, so the override
    # must be read straight off disk — otherwise re-running init follows the
    # github.com URL host and runs the wrong (GitHub) auth checks.
    awf_dir = tmp_path / ".awf"
    awf_dir.mkdir()
    (awf_dir / "workspace.yml").write_text("forge: bitbucket\n", encoding="utf-8")
    _stub_origin(monkeypatch, "https://github.com/acme/widgets.git")
    _forbid_check_gh(monkeypatch)
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda *_a, **_k: {
            "BITBUCKET_API_TOKEN": "tok",
            "BITBUCKET_EMAIL": "dev@example.com",
            "BITBUCKET_AUTH_MODE": "basic",
        },
    )

    result = _runner.invoke(app, ["init", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    forge = json.loads(result.output)["forge"]
    # Explicit on-disk ``forge:`` beats the github.com URL host.
    assert forge["forge"] == "bitbucket"
    assert forge["auth_ok"] is True


@pytest.mark.unit
def test_init_unknown_host_defaults_to_github_without_hard_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_prereqs(monkeypatch)
    _stub_origin(monkeypatch, "https://gitlab.com/acme/widgets.git")
    _stub_check_gh(monkeypatch, present=True)
    _stub_github_readiness(monkeypatch, ok=True)

    result = _runner.invoke(app, ["init", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output  # unknown host does not hard-fail
    forge = json.loads(result.output)["forge"]
    assert forge["forge"] == "github"  # forge.py default for an unknown host
    assert forge["host_detected"] is False
    assert forge["host"] == "gitlab.com"

    pretty = _runner.invoke(app, ["init", str(tmp_path)])
    assert "not recognized" in pretty.output
    assert "gitlab.com" in pretty.output


@pytest.mark.unit
def test_init_forge_block_present_on_write_yes_json_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_prereqs(monkeypatch)
    _stub_origin(monkeypatch, "https://github.com/acme/widgets.git")
    _stub_check_gh(monkeypatch, present=True)
    _stub_github_readiness(monkeypatch, ok=True)

    result = _runner.invoke(
        app,
        ["init", str(tmp_path), "--write-profile", "--yes", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "write"
    # The forge block + per-signal auth status travels in the JSON payload, not
    # only the pretty echo.
    assert payload["forge"]["forge"] == "github"
    assert payload["forge"]["auth"]["github_readiness"] == "ok"
