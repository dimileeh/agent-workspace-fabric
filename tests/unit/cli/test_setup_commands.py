"""CLI coverage for the ``awf setup`` read-only host readiness pass."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from awf.cli import setup_commands
from awf.cli.main import app
from awf.host_setup.config import HostSetupConfig, HostSetupConfigError
from awf.host_setup.source_assets import SOURCE_CHECKOUT_MARKERS
from awf.host_setup.system_checks import SetupCheckLevel, SetupCheckResult

_runner = CliRunner()


def _ok(name: str) -> SetupCheckResult:
    return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")


def _all_ok(**_kwargs: object) -> list[SetupCheckResult]:
    return [_ok("docker"), _ok("compose"), _ok("git")]


def _docker_blocked(**_kwargs: object) -> list[SetupCheckResult]:
    return [
        SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.BLOCKED,
            summary="Docker is installed but the daemon is not reachable.",
            detail="`docker info` failed.",
            fix="Start Docker Desktop or the Docker daemon.",
            docs_link="https://docs.docker.com/config/daemon/",
            data={"daemon": False},
        ),
        SetupCheckResult(
            name="gh",
            level=SetupCheckLevel.WARNING,
            summary="GitHub CLI (gh) is not installed.",
            detail="gh missing.",
            fix="Install gh.",
        ),
    ]


@dataclass
class _Harness:
    writes: list[HostSetupConfig] = field(default_factory=list)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Isolate config IO and default the host checks to all-OK."""
    state = _Harness()
    monkeypatch.setattr(setup_commands, "read_host_setup_config", lambda **_kw: HostSetupConfig())

    def fake_write(config: HostSetupConfig, **_kw: object) -> None:
        state.writes.append(config)

    monkeypatch.setattr(setup_commands, "write_host_setup_config", fake_write)
    monkeypatch.setattr(setup_commands, "run_system_checks", _all_ok)
    return state


def _make_source_checkout(root: Path) -> Path:
    """Materialize every required AWF source-checkout marker under ``root``."""
    for marker in SOURCE_CHECKOUT_MARKERS:
        target = root / marker.path
        if marker.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
    return root


# --- Help -----------------------------------------------------------------


@pytest.mark.unit
def test_setup_help_describes_readiness_pass() -> None:
    """Verify setup help describes the read-only readiness pass and options."""
    result = _runner.invoke(app, ["setup", "--help"], env={"COLUMNS": "200"})
    visible_help = click.unstyle(result.output)

    assert result.exit_code == 0, result.output
    assert "Prepare this machine for AWF" in visible_help
    assert "--dry-run" in visible_help
    assert "--provider" in visible_help
    assert "--source-checkout" in visible_help
    assert "Traceback" not in visible_help


# --- Rendering / A7 -------------------------------------------------------


@pytest.mark.unit
def test_setup_dry_run_pretty_includes_status_blockers_docs_next(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify pretty output carries status, reason, problem/cause/fix/docs, next."""
    monkeypatch.setattr(setup_commands, "run_system_checks", _docker_blocked)
    result = _runner.invoke(app, ["setup", "--dry-run"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Status: blocked" in result.stderr
    assert "Command: awf setup" in result.stderr
    assert "Reason: SETUP_READINESS_FAILED" in result.stderr
    assert "Problem:" in result.stderr
    assert "Cause:" in result.stderr
    assert "Fix:" in result.stderr
    assert "Docs:" in result.stderr
    assert "Next:" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.unit
def test_setup_dry_run_json_success_shape(harness: _Harness) -> None:
    """Verify an all-OK dry-run renders a success JSON payload and exits 0."""
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["command"] == "awf setup"
    assert payload["details"]["dry_run"] is True
    assert payload["next_steps"]


# --- Provider selectors ---------------------------------------------------


@pytest.mark.unit
def test_setup_no_selector_runs_checks_without_provider_mutation(harness: _Harness) -> None:
    """Verify no selector runs host checks and forwards an empty selection."""
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["details"]["selected_providers"] == []
    assert harness.writes == []


@pytest.mark.unit
def test_setup_provider_github_forwarded(harness: _Harness) -> None:
    """Verify a single provider selector is forwarded into the payload (A2)."""
    result = _runner.invoke(app, ["setup", "--provider", "github", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["details"]["selected_providers"] == ["github"]


@pytest.mark.unit
def test_setup_repeated_providers_deduped_and_ordered(harness: _Harness) -> None:
    """Verify repeated/aliased selectors de-dupe while preserving order."""
    result = _runner.invoke(
        app,
        [
            "setup",
            "--provider",
            "github",
            "--provider",
            "claude",
            "--provider",
            "github",
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["details"]["selected_providers"] == ["github", "claude_code"]


@pytest.mark.unit
def test_setup_unknown_provider_rejected_without_fallback(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify an unknown provider fails reason-coded with no all-provider run (A4)."""
    called = {"checks": False}

    def guard(**_kw: object) -> list[SetupCheckResult]:
        called["checks"] = True
        return _all_ok()

    monkeypatch.setattr(setup_commands, "run_system_checks", guard)
    result = _runner.invoke(app, ["setup", "--provider", "bogus", "--dry-run", "--format", "json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "SETUP_PROVIDER_UNKNOWN"
    assert called["checks"] is False


# --- Plain-secret consent / A3 -------------------------------------------


@pytest.mark.unit
def test_setup_allow_plain_secrets_forwarded_in_dry_run(harness: _Harness) -> None:
    """Verify the plain-file consent flag is forwarded without writing (A1/A3)."""
    result = _runner.invoke(
        app, ["setup", "--allow-plain-secrets", "--dry-run", "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["details"]["plain_file_consent"] is True
    assert harness.writes == []


@pytest.mark.unit
def test_setup_non_dry_run_persists_plain_secret_consent(harness: _Harness) -> None:
    """Verify non-dry-run persists plain-file consent; default leaves it false (A3)."""
    consented = _runner.invoke(app, ["setup", "--allow-plain-secrets", "--format", "json"])
    assert consented.exit_code == 0, consented.output
    assert harness.writes[-1].consent.plain_file_secrets is True

    default = _runner.invoke(app, ["setup", "--format", "json"])
    assert default.exit_code == 0, default.output
    assert default.exit_code == 0
    assert harness.writes[-1].consent.plain_file_secrets is False


# --- Docker missing / A5 --------------------------------------------------


@pytest.mark.unit
def test_setup_docker_missing_returns_readiness_failure(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a Docker blocker returns SETUP_READINESS_FAILED with next actions (A5)."""
    monkeypatch.setattr(setup_commands, "run_system_checks", _docker_blocked)
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "SETUP_READINESS_FAILED"
    assert payload["next_steps"]


# --- Source checkout / A6 -------------------------------------------------


@pytest.mark.unit
def test_setup_source_checkout_valid_resolves(harness: _Harness, tmp_path: Path) -> None:
    """Verify a complete source checkout validates and is recorded in details (A6)."""
    root = _make_source_checkout(tmp_path / "awf")
    result = _runner.invoke(
        app,
        ["setup", "--dry-run", "--source-checkout", str(root), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "source_checkout" in payload["details"]
    assert payload["status"] == "success"


@pytest.mark.unit
def test_setup_source_checkout_invalid_blocks_with_missing_markers(
    harness: _Harness, tmp_path: Path
) -> None:
    """Verify an invalid source checkout blocks with missing markers (A6)."""
    empty = tmp_path / "not-awf"
    empty.mkdir()
    result = _runner.invoke(
        app,
        ["setup", "--dry-run", "--source-checkout", str(empty), "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    source_issues = [
        issue for issue in payload["issues"] if issue["reason_code"] == "SOURCE_CHECKOUT_INVALID"
    ]
    assert source_issues
    assert source_issues[0]["details"]["missing_markers"]


# --- Dry-run no mutation / A1 --------------------------------------------


@pytest.mark.unit
def test_setup_dry_run_never_writes_config(harness: _Harness) -> None:
    """Verify dry-run never writes config (A1)."""
    result = _runner.invoke(
        app, ["setup", "--allow-plain-secrets", "--dry-run", "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    assert harness.writes == []


@pytest.mark.unit
def test_setup_non_dry_run_writes_safe_config(harness: _Harness) -> None:
    """Verify a non-dry-run run persists a safe (non-secret) config once."""
    result = _runner.invoke(app, ["setup", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert len(harness.writes) == 1
    assert isinstance(harness.writes[0], HostSetupConfig)
    assert harness.writes[0].source_checkout is None


@pytest.mark.unit
def test_setup_non_dry_run_persists_source_checkout_metadata(
    harness: _Harness, tmp_path: Path
) -> None:
    """Verify a non-dry-run run records verified source-checkout metadata + consent."""
    root = _make_source_checkout(tmp_path / "awf")
    result = _runner.invoke(app, ["setup", "--source-checkout", str(root), "--format", "json"])

    assert result.exit_code == 0, result.output
    written = harness.writes[-1]
    assert written.source_checkout is not None
    assert written.consent.source_checkout_assets is True


# --- Non-interactive / A8 -------------------------------------------------


@pytest.mark.unit
def test_setup_non_interactive_provider_requires_input(harness: _Harness) -> None:
    """Verify a non-interactive provider-config path returns the input signal (A8)."""
    result = _runner.invoke(
        app,
        ["setup", "--provider", "github", "--non-interactive", "--format", "json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "INTERACTIVE_INPUT_REQUIRED"
    assert harness.writes == []


@pytest.mark.unit
def test_setup_non_interactive_provider_surfaces_readiness_blockers(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify readiness blockers win over the interactive-input guard.

    Regression for PRRT_kwDOSJAM6s6F6Jvu: a non-dry-run
    ``--provider ... --non-interactive`` run must still surface
    SETUP_READINESS_FAILED blockers (e.g. missing Docker) rather than masking
    them behind INTERACTIVE_INPUT_REQUIRED, so the operator sees the host
    problem they must fix first instead of a misleading input-required exit.
    """
    monkeypatch.setattr(setup_commands, "run_system_checks", _docker_blocked)
    result = _runner.invoke(
        app,
        ["setup", "--provider", "github", "--non-interactive", "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    reason_codes = [issue["reason_code"] for issue in payload["issues"]]
    assert "SETUP_READINESS_FAILED" in reason_codes
    assert "INTERACTIVE_INPUT_REQUIRED" not in reason_codes


# --- Corrupt config defensive handling ------------------------------------


@pytest.mark.unit
def test_setup_corrupt_config_blocks(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a corrupt host config surfaces a reason-coded blocker, not a traceback."""

    def raise_corrupt(**_kw: object) -> HostSetupConfig:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_CORRUPT",
            message="Host setup config is corrupt or unsupported.",
            path=Path("/tmp/.awf/config.yml"),
        )

    monkeypatch.setattr(setup_commands, "read_host_setup_config", raise_corrupt)
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "HOST_SETUP_CONFIG_CORRUPT"
    # The config file path operators must fix is preserved in the issue details
    # rather than dropped (it lives on the error's ``path``, not ``details``).
    assert payload["issues"][0]["details"]["path"] == "/tmp/.awf/config.yml"


@pytest.mark.unit
def test_setup_write_failure_blocks_includes_path(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a failed config write surfaces its path alongside existing details."""

    def raise_write_failed(_config: HostSetupConfig, **_kw: object) -> None:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_WRITE_FAILED",
            message="Failed to write host setup config.",
            path=Path("/tmp/.awf/config.yml"),
            details={"error_type": "PermissionError"},
        )

    monkeypatch.setattr(setup_commands, "write_host_setup_config", raise_write_failed)
    result = _runner.invoke(app, ["setup", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "HOST_SETUP_CONFIG_WRITE_FAILED"
    details = payload["issues"][0]["details"]
    assert details["path"] == "/tmp/.awf/config.yml"
    # The merged path must not clobber pre-existing diagnostic details.
    assert details["error_type"] == "PermissionError"


@pytest.mark.unit
def test_setup_write_failure_preserves_readiness_report_json(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a failed config write still surfaces host-check blockers/warnings.

    Regression for PRRT_kwDOSJAM6s6F6DjR: a non-dry-run write happens after the
    host checks finish, so a write failure must not hide the readiness report
    (blockers, warnings, and check facts) the operator ran setup to see.
    """
    monkeypatch.setattr(setup_commands, "run_system_checks", _docker_blocked)

    def raise_write_failed(_config: HostSetupConfig, **_kw: object) -> None:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_WRITE_FAILED",
            message="Unable to write host setup config.",
            path=Path("/tmp/.awf/config.yml"),
            details={"error_type": "PermissionError"},
        )

    monkeypatch.setattr(setup_commands, "write_host_setup_config", raise_write_failed)
    result = _runner.invoke(app, ["setup", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    reason_codes = [issue["reason_code"] for issue in payload["issues"]]
    # The host-check readiness issues survive alongside the config-write failure.
    assert "SETUP_READINESS_FAILED" in reason_codes
    assert "HOST_SETUP_CONFIG_WRITE_FAILED" in reason_codes
    write_issues = [
        issue
        for issue in payload["issues"]
        if issue["reason_code"] == "HOST_SETUP_CONFIG_WRITE_FAILED"
    ]
    # The write-failure path/diagnostic details remain available to fix it.
    assert write_issues[0]["details"]["path"] == "/tmp/.awf/config.yml"
    assert write_issues[0]["details"]["error_type"] == "PermissionError"
    # The host-check provenance (the per-check levels) stays in the payload.
    assert payload["details"]["checks"]


@pytest.mark.unit
def test_setup_write_failure_preserves_readiness_report_pretty(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify pretty output also keeps both the readiness and write diagnostics."""
    monkeypatch.setattr(setup_commands, "run_system_checks", _docker_blocked)

    def raise_write_failed(_config: HostSetupConfig, **_kw: object) -> None:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_WRITE_FAILED",
            message="Unable to write host setup config.",
            path=Path("/tmp/.awf/config.yml"),
            details={"error_type": "PermissionError"},
        )

    monkeypatch.setattr(setup_commands, "write_host_setup_config", raise_write_failed)
    result = _runner.invoke(app, ["setup", "--format", "pretty"])

    assert result.exit_code == 1
    assert "Status: blocked" in result.stderr
    assert "SETUP_READINESS_FAILED" in result.stderr
    assert "HOST_SETUP_CONFIG_WRITE_FAILED" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.unit
def test_setup_config_error_pretty_includes_path(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify pretty output also reports the config path operators need to fix."""

    def raise_corrupt(**_kw: object) -> HostSetupConfig:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_CORRUPT",
            message="Host setup config is corrupt or unsupported.",
            path=Path("/tmp/.awf/config.yml"),
        )

    monkeypatch.setattr(setup_commands, "read_host_setup_config", raise_corrupt)
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "pretty"])

    assert result.exit_code == 1
    assert "path: /tmp/.awf/config.yml" in result.stderr
