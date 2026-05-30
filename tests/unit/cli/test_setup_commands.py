"""CLI coverage for the ``awf setup`` first-run wizard shell."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

import awf.cli.setup_commands as setup_commands
from awf.cli.main import app
from awf.host_setup.config import HostSetupConfig, HostSetupConfigError
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_INVALID,
    validate_source_checkout,
)
from awf.host_setup.system_checks import SystemCheck, SystemChecksReport

_runner = CliRunner()


def _report(*checks: SystemCheck) -> SystemChecksReport:
    return SystemChecksReport(checks=checks)


def _healthy_report() -> SystemChecksReport:
    return _report(
        SystemCheck(
            id="docker_cli",
            label="Docker CLI",
            status="ok",
            reason="DOCKER_CLI_AVAILABLE",
            message="ok",
        ),
        SystemCheck(id="git", label="Git CLI", status="ok", reason="GIT_AVAILABLE", message="ok"),
    )


def _docker_blocked_report() -> SystemChecksReport:
    return _report(
        SystemCheck(
            id="docker_cli",
            label="Docker CLI",
            status="fail",
            reason="DOCKER_CLI_NOT_FOUND",
            message="Docker CLI was not found on PATH.",
            fix="Install Docker Desktop or make the docker CLI available.",
            docs_link="https://docs.docker.com/get-docker/",
        ),
        SystemCheck(id="git", label="Git CLI", status="ok", reason="GIT_AVAILABLE", message="ok"),
    )


@pytest.fixture
def patched_setup(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch config IO + system checks so the command is deterministic."""
    state: dict[str, object] = {
        "report": _healthy_report(),
        "writes": [],
        "system_check_calls": 0,
    }

    def _fake_run_system_checks(**_kwargs: object) -> SystemChecksReport:
        state["system_check_calls"] = int(state["system_check_calls"]) + 1  # type: ignore[call-overload]
        return state["report"]  # type: ignore[return-value]

    def _fake_read() -> HostSetupConfig:
        return HostSetupConfig()

    def _fake_write(config: HostSetupConfig, **_kwargs: object) -> None:
        writes = state["writes"]
        assert isinstance(writes, list)
        writes.append(config)

    monkeypatch.setattr(setup_commands, "run_system_checks", _fake_run_system_checks)
    monkeypatch.setattr(setup_commands, "read_host_setup_config", _fake_read)
    monkeypatch.setattr(setup_commands, "write_host_setup_config", _fake_write)
    return state


@pytest.mark.unit
def test_setup_help_describes_real_first_run_surface() -> None:
    """Verify setup help advertises the real wizard surface, not a placeholder."""
    result = _runner.invoke(app, ["setup", "--help"], env={"COLUMNS": "200"})
    visible_help = click.unstyle(result.output)

    assert result.exit_code == 0, result.output
    assert "Prepare this machine for AWF" in visible_help
    assert "--dry-run" in visible_help
    assert "--provider" in visible_help
    assert "Reserved" not in visible_help
    assert "Traceback" not in visible_help


@pytest.mark.unit
def test_setup_dry_run_ready_success(patched_setup: dict[str, object]) -> None:
    """Verify a healthy dry-run reports success and the next command."""
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "success"
    assert payload["details"]["dry_run"] is True
    assert any("awf start" in step for step in payload["next_steps"])


@pytest.mark.unit
def test_setup_dry_run_does_not_write_config_or_secrets(patched_setup: dict[str, object]) -> None:
    """Verify a dry-run never persists config."""
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert patched_setup["writes"] == []


@pytest.mark.unit
def test_setup_dry_run_docker_blocker_readiness_failure(patched_setup: dict[str, object]) -> None:
    """Verify a docker blocker yields a reason-coded readiness failure."""
    patched_setup["report"] = _docker_blocked_report()
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "SETUP_READINESS_FAILED"
    assert "docker_cli" in payload["issues"][0]["details"]["blockers"]
    assert payload["next_steps"]


@pytest.mark.unit
def test_setup_docker_blocker_pretty_goes_to_stderr(patched_setup: dict[str, object]) -> None:
    """Verify blocked pretty output goes to stderr with an empty stdout."""
    patched_setup["report"] = _docker_blocked_report()
    result = _runner.invoke(app, ["setup", "--dry-run"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Status: blocked" in result.stderr
    assert "Docs:" in result.stderr
    assert "Next:" in result.stderr


@pytest.mark.unit
def test_setup_provider_selector_forwarded(patched_setup: dict[str, object]) -> None:
    """Verify a single provider selector becomes a targeted recheck scope."""
    result = _runner.invoke(app, ["setup", "--dry-run", "--provider", "github", "--format", "json"])

    assert result.exit_code == 0, result.output
    details = json.loads(result.output)["details"]
    assert details["providers"] == ["github"]
    assert details["provider_scope"] == "targeted recheck"


@pytest.mark.unit
def test_setup_repeated_provider_selectors_deduped(patched_setup: dict[str, object]) -> None:
    """Verify repeated provider selectors are de-duplicated."""
    result = _runner.invoke(
        app,
        ["setup", "--dry-run", "--provider", "github", "--provider", "github", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["details"]["providers"] == ["github"]


@pytest.mark.unit
def test_setup_multiple_providers_forwarded(patched_setup: dict[str, object]) -> None:
    """Verify multiple provider selectors are forwarded order-stable."""
    result = _runner.invoke(
        app,
        ["setup", "--dry-run", "--provider", "github", "--provider", "codex", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["details"]["providers"] == ["github", "codex"]


@pytest.mark.unit
def test_setup_claude_alias_normalized(patched_setup: dict[str, object]) -> None:
    """Verify the ``claude`` alias normalizes to ``claude_code``."""
    result = _runner.invoke(app, ["setup", "--dry-run", "--provider", "claude", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["details"]["providers"] == ["claude_code"]


@pytest.mark.unit
def test_setup_unknown_provider_rejected(patched_setup: dict[str, object]) -> None:
    """Verify an unknown provider is a usage error that skips system checks."""
    result = _runner.invoke(app, ["setup", "--dry-run", "--provider", "nope", "--format", "json"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["reason_code"] == "SETUP_PROVIDER_UNKNOWN"
    assert payload["issues"][0]["details"]["unknown_providers"] == ["nope"]
    assert patched_setup["system_check_calls"] == 0


@pytest.mark.unit
def test_setup_allow_plain_secrets_forwarded_not_default(patched_setup: dict[str, object]) -> None:
    """Verify the plain-secret consent flag is forwarded and defaults off."""
    with_flag = _runner.invoke(
        app, ["setup", "--dry-run", "--allow-plain-secrets", "--format", "json"]
    )
    without_flag = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert with_flag.exit_code == 0, with_flag.output
    assert json.loads(with_flag.output)["details"]["allow_plain_file_credentials"] is True
    assert json.loads(without_flag.output)["details"]["allow_plain_file_credentials"] is False


@pytest.mark.unit
def test_setup_source_checkout_valid_dry_run(patched_setup: dict[str, object]) -> None:
    """Verify a valid source checkout is recorded in readiness details."""
    repo_root = str(Path(__file__).resolve().parents[3])
    result = _runner.invoke(
        app,
        ["setup", "--dry-run", "--source-checkout", repo_root, "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    details = json.loads(result.output)["details"]
    # validate_source_checkout resolves the canonical root before recording it.
    assert details["source_checkout"] == str(validate_source_checkout(repo_root).root)


@pytest.mark.unit
def test_setup_source_checkout_invalid(
    patched_setup: dict[str, object],
    tmp_path: Path,
) -> None:
    """Verify an invalid source checkout surfaces the reason and missing markers."""
    result = _runner.invoke(
        app,
        ["setup", "--dry-run", "--source-checkout", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["reason_code"] == SOURCE_CHECKOUT_INVALID
    assert payload["issues"][0]["details"]["missing_markers"]


@pytest.mark.unit
def test_setup_non_interactive_provider_requires_input(patched_setup: dict[str, object]) -> None:
    """Verify non-interactive provider setup without dry-run requires input."""
    result = _runner.invoke(
        app,
        ["setup", "--non-interactive", "--provider", "github", "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["reason_code"] == "INTERACTIVE_INPUT_REQUIRED"
    assert patched_setup["system_check_calls"] == 0


@pytest.mark.unit
def test_setup_non_dry_run_writes_safe_config(patched_setup: dict[str, object]) -> None:
    """Verify a healthy non-dry-run persists safe, non-secret config."""
    result = _runner.invoke(app, ["setup", "--allow-plain-secrets", "--format", "json"])

    assert result.exit_code == 0, result.output
    writes = patched_setup["writes"]
    assert isinstance(writes, list) and len(writes) == 1
    written = writes[0]
    assert isinstance(written, HostSetupConfig)
    assert written.consent.plain_file_secrets is True
    # No secret values are persisted (model would reject them on write anyway).
    assert "secret" not in json.dumps(written.model_dump(mode="json")).replace(
        "plain_file_secrets", ""
    )


@pytest.mark.unit
def test_setup_config_corrupt_surfaces_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a corrupt host setup config surfaces its reason code."""

    def _raise() -> HostSetupConfig:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_CORRUPT",
            message="Host setup config is corrupt or unsupported.",
            path=Path("~/.awf/config.yml"),
            details={"error_type": "non_mapping_yaml"},
        )

    calls = {"system": 0}

    def _fake_run_system_checks(**_kwargs: object) -> SystemChecksReport:
        calls["system"] += 1
        return _healthy_report()

    monkeypatch.setattr(setup_commands, "read_host_setup_config", _raise)
    monkeypatch.setattr(setup_commands, "run_system_checks", _fake_run_system_checks)

    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["reason_code"] == "HOST_SETUP_CONFIG_CORRUPT"
    assert calls["system"] == 0


@pytest.mark.unit
def test_setup_json_and_pretty_shapes(patched_setup: dict[str, object]) -> None:
    """Verify JSON keys are stable and pretty output renders the key sections."""
    patched_setup["report"] = _docker_blocked_report()
    json_result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])
    pretty_result = _runner.invoke(app, ["setup", "--dry-run"])

    payload = json.loads(json_result.output)
    assert set(payload) >= {"status", "command", "summary", "reason_code", "issues", "next_steps"}
    assert payload["command"] == "awf setup"

    stderr = pretty_result.stderr
    assert "Status: blocked" in stderr
    assert "Docs:" in stderr
    assert "Next:" in stderr
    assert "docker_cli" in stderr
