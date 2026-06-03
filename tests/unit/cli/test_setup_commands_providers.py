"""CLI coverage for the ``awf setup`` read-only host readiness pass.

Provider orchestration integration, corrupt-config defensive handling,
write-failure cases, and the client env-file existence guard. Earlier
readiness/selector coverage lives in the sibling part.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.cli import setup_commands
from awf.cli.main import app
from awf.common.audit import REDACTION_MARKER
from awf.host_setup.config import HostSetupConfig, HostSetupConfigError
from awf.host_setup.providers import (
    ProviderSetupResult,
    ProviderSetupSummary,
)
from awf.host_setup.rendering import (
    START_COMPOSE_ASSETS_MISSING,
)
from awf.host_setup.source_assets import (
    validate_source_checkout,
)
from tests.unit.cli._setup_commands_shared import (
    _docker_blocked,
    _Harness,
    _make_source_checkout,
    _runner,
)

# --- Provider orchestration integration (T07) -----------------------------


def _ready_github_summary(
    _settings: object,
    *,
    selected_providers: list[str],
    config: HostSetupConfig,
    **_kwargs: object,
) -> tuple[ProviderSetupSummary, HostSetupConfig]:
    """Fake orchestration that marks GitHub ready via gh (no token stored).

    A token-shaped value is planted on ``backend`` -- a field that
    ``ProviderSetupSummary.to_details()`` actually emits and the renderer prints
    under ``Details:`` -- so the no-token assertion in
    ``test_setup_provider_github_pretty_prints_no_token`` exercises the renderer's
    redaction rather than passing vacuously. Planting it on the free-text
    ``summary`` would be vacuous: ``to_details()`` drops that field, so the token
    would never reach the rendered payload and the assertion could not regress.
    """
    summary = ProviderSetupSummary(
        mode="targeted_recheck" if selected_providers else "all_providers",
        selected=tuple(selected_providers),
        providers=(
            ProviderSetupResult(
                name="github",
                status="ready",
                reason_code="GITHUB_GH_AUTH_OK",
                summary="GitHub is ready via gh CLI authentication.",
                backend="ghp_should_be_redacted",
                configured=True,
                rechecked=True,
            ),
        ),
        overall_status="ready",
    )
    return summary, config


def _failed_provider_summary(
    _settings: object,
    *,
    selected_providers: list[str],
    config: HostSetupConfig,
    **_kwargs: object,
) -> tuple[ProviderSetupSummary, HostSetupConfig]:
    """Fake orchestration where the selected provider auth failed (non-blocking)."""
    summary = ProviderSetupSummary(
        mode="targeted_recheck",
        selected=tuple(selected_providers),
        providers=(
            ProviderSetupResult(
                name="github",
                status="unavailable",
                reason_code="PROVIDER_SETUP_AUTH_INVALID",
                summary="GitHub auth via GH_TOKEN is not usable.",
            ),
        ),
        overall_status="not_ready",
    )
    return summary, config


@pytest.mark.unit
def test_setup_provider_github_targeted_recheck_summary(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-dry-run ``--provider github`` folds a targeted-recheck provider summary."""
    monkeypatch.setattr(setup_commands, "orchestrate_provider_setup", _ready_github_summary)
    result = _runner.invoke(app, ["setup", "--provider", "github", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    providers = payload["details"]["providers"]
    assert providers["mode"] == "targeted_recheck"
    assert providers["providers"]["github"]["status"] == "ready"


@pytest.mark.unit
def test_setup_provider_github_pretty_prints_no_token(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pretty (stderr) output for a provider run never prints a raw token."""
    monkeypatch.setattr(setup_commands, "orchestrate_provider_setup", _ready_github_summary)
    result = _runner.invoke(app, ["setup", "--provider", "github"])

    assert result.exit_code == 0, result.output
    # ``_ready_github_summary`` plants a ``ghp_`` token shape on the rendered
    # ``backend`` field, so the renderer must have actively redacted it (the
    # marker is present) rather than the value simply never reaching stderr.
    assert REDACTION_MARKER in result.stderr
    assert "ghp_" not in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.unit
def test_setup_failed_provider_is_non_blocking(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed provider marks itself unavailable without blocking the process."""
    monkeypatch.setattr(setup_commands, "orchestrate_provider_setup", _failed_provider_summary)
    result = _runner.invoke(app, ["setup", "--provider", "github", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    providers = payload["details"]["providers"]["providers"]
    assert providers["github"]["status"] == "unavailable"


@pytest.mark.unit
def test_setup_all_provider_run_labels_all_providers(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-dry-run run with no selector labels the summary all_providers."""

    def _all_summary(
        _settings: object,
        *,
        selected_providers: list[str],
        config: HostSetupConfig,
        **_kwargs: object,
    ) -> tuple[ProviderSetupSummary, HostSetupConfig]:
        summary = ProviderSetupSummary(
            mode="all_providers",
            selected=(),
            providers=(),
            overall_status="not_ready",
        )
        return summary, config

    monkeypatch.setattr(setup_commands, "orchestrate_provider_setup", _all_summary)
    result = _runner.invoke(app, ["setup", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["details"]["providers"]["mode"] == "all_providers"


@pytest.mark.unit
def test_resolve_provider_settings_builds_service_settings() -> None:
    """The provider settings helper resolves real service settings from an environ."""
    settings = setup_commands._resolve_provider_settings({})
    assert settings is not None
    assert hasattr(settings, "host_home")


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
def test_setup_config_error_payload_carries_next_steps(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a HostSetupConfigError payload carries next-step guidance.

    Regression for review comment issue:4585200251: config-error exits flow
    through ``_reason_coded_payload`` too, so they must surface the same
    machine-readable ``next_steps`` the happy path always provides.
    """

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
    assert payload["next_steps"]


@pytest.mark.unit
def test_setup_secret_config_preserves_field_path(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a field-level diagnostic path survives the config-file-path merge.

    Regression for PRRT_kwDOSJAM6s6F8eOX: when a secret-bearing (or recursive)
    config error already reports the offending YAML field under ``details.path``
    (for example ``providers.github.token``), merging the config file path must
    not clobber it -- the file path moves to ``config_path`` so the operator is
    still told which field to remove.
    """

    def raise_secret(**_kw: object) -> HostSetupConfig:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_SECRET_VALUE",
            message="Host setup config contains a secret value or secret-bearing key.",
            path=Path("/tmp/.awf/config.yml"),
            details={"issue": "secret-bearing key", "path": "providers.github.token"},
        )

    monkeypatch.setattr(setup_commands, "read_host_setup_config", raise_secret)
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "HOST_SETUP_CONFIG_SECRET_VALUE"
    details = payload["issues"][0]["details"]
    # The offending YAML field path is preserved, not overwritten.
    assert details["path"] == "providers.github.token"
    assert details["issue"] == "secret-bearing key"
    # The config file path is still surfaced, under a distinct key.
    assert details["config_path"] == "/tmp/.awf/config.yml"


@pytest.mark.unit
def test_setup_explicit_source_checkout_dry_run_skips_corrupt_config(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corrupt host config must not abort an explicit --source-checkout dry-run probe.

    Regression for PRRT_kwDOSJAM6s6F-Zv0: an explicit ``--source-checkout`` is
    validated without reading host config (mirroring ``awf start``'s
    ``_resolve_start_source_checkout``), so a corrupt or secret-bearing
    ``~/.awf/config.yml`` the read-only dry-run probe never consumes cannot block
    it. Host config is only read where it is actually needed -- to resolve
    persisted source metadata (no explicit checkout) or to persist safe config
    (non-dry-run) -- so this path never opens ``read_host_setup_config``.
    """
    reads: list[object] = []

    def raise_corrupt(**_kw: object) -> HostSetupConfig:
        reads.append(object())
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_CORRUPT",
            message="Host setup config is corrupt or unsupported.",
            path=Path("/tmp/.awf/config.yml"),
        )

    monkeypatch.setattr(setup_commands, "read_host_setup_config", raise_corrupt)
    root = _make_source_checkout(tmp_path / "awf")
    result = _runner.invoke(
        app,
        ["setup", "--dry-run", "--source-checkout", str(root), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert "source_checkout" in payload["details"]
    # The explicit-checkout dry-run path never reads host config, so the corrupt
    # config is never even opened.
    assert reads == []


@pytest.mark.unit
def test_setup_explicit_source_checkout_non_dry_run_still_blocks_corrupt_config(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-dry-run explicit-checkout setup still blocks on a corrupt host config.

    Companion to ``test_setup_explicit_source_checkout_dry_run_skips_corrupt_config``:
    the dry-run skip is deliberately narrow. A non-dry-run run persists safe
    config, which must read and merge the existing on-disk config, so a corrupt
    ``~/.awf/config.yml`` remains a legitimate reason-coded blocker rather than
    being silently clobbered.
    """

    def raise_corrupt(**_kw: object) -> HostSetupConfig:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_CORRUPT",
            message="Host setup config is corrupt or unsupported.",
            path=Path("/tmp/.awf/config.yml"),
        )

    monkeypatch.setattr(setup_commands, "read_host_setup_config", raise_corrupt)
    root = _make_source_checkout(tmp_path / "awf")
    result = _runner.invoke(
        app,
        ["setup", "--source-checkout", str(root), "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "HOST_SETUP_CONFIG_CORRUPT"


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
def test_setup_write_failure_next_steps_keep_dry_run_guidance(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the config-write failure path steers operators to re-run --dry-run.

    Regression for issue:4585200251: every blocked readiness path tells the
    operator to ``re-run awf setup --dry-run`` (so they re-verify the host
    without re-attempting the write), but the config-write failure path used to
    omit ``--dry-run`` and steer them toward a bare ``awf setup`` that would
    retry the failing write. The folded write-failure guidance must stay
    consistent with the canonical blocked-host guidance.
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
    assert payload["next_steps"] == [
        "Fix the reported blockers above, then re-run awf setup --dry-run.",
    ]


@pytest.mark.unit
def test_setup_write_failure_preserves_field_path(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the folded write-failure issue keeps a field-level diagnostic path.

    Regression for PRRT_kwDOSJAM6s6F8eOX on the non-dry-run write path: when the
    write error already reports the offending YAML field under ``details.path``,
    folding the config file path in must not clobber it.
    """

    def raise_secret_write(_config: HostSetupConfig, **_kw: object) -> None:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_SECRET_VALUE",
            message="Host setup config contains a secret value or secret-bearing key.",
            path=Path("/tmp/.awf/config.yml"),
            details={"issue": "secret-bearing key", "path": "providers.github.token"},
        )

    monkeypatch.setattr(setup_commands, "write_host_setup_config", raise_secret_write)
    result = _runner.invoke(app, ["setup", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    write_issues = [
        issue
        for issue in payload["issues"]
        if issue["reason_code"] == "HOST_SETUP_CONFIG_SECRET_VALUE"
    ]
    details = write_issues[0]["details"]
    # The offending YAML field path is preserved; the file path uses config_path.
    assert details["path"] == "providers.github.token"
    assert details["config_path"] == "/tmp/.awf/config.yml"


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


# --- T08: client env-file existence guard (PRRT_kwDOSJAM6s6G4DGD) ----------


@pytest.mark.unit
def test_resolve_client_env_file_require_existing_raises_when_absent(tmp_path: Path) -> None:
    """Regression for PRRT_kwDOSJAM6s6G4DGD: a fresh, valid source checkout (only
    ``.env.example`` present) resolves a ``docker/compose/.env`` that does not
    exist. A non-dry-run apply (``require_existing``) must raise rather than pin an
    MCP ``--env-file`` ``awf mcp serve`` would reject with "env file does not
    exist"."""
    root = _make_source_checkout(tmp_path / "awf")
    compose_env = validate_source_checkout(root).root / "docker" / "compose" / ".env"
    assert not compose_env.exists()

    with pytest.raises(setup_commands.ClientEnvFileMissingError) as excinfo:
        setup_commands._resolve_client_env_file(root, True)

    assert excinfo.value.env_file == compose_env


@pytest.mark.unit
def test_resolve_client_env_file_require_existing_returns_when_present(tmp_path: Path) -> None:
    """Verify the existence guard returns the resolved path when it exists on disk
    (a bootstrapped checkout's root ``.env`` fallback), so a real apply registers a
    usable ``--env-file``."""
    root = _make_source_checkout(tmp_path / "awf")
    root_env = root / ".env"
    root_env.write_text("AWF_API_TOKEN=x\n", encoding="utf-8")

    resolved = setup_commands._resolve_client_env_file(root, True)

    assert resolved == root_env


@pytest.mark.unit
def test_resolve_client_env_file_dry_run_keeps_absent_path(tmp_path: Path) -> None:
    """Verify a dry-run resolution (``require_existing`` unset) keeps resolving the
    absent compose ``.env`` path, since the diff never starts the MCP server."""
    root = _make_source_checkout(tmp_path / "awf")
    compose_env = validate_source_checkout(root).root / "docker" / "compose" / ".env"

    resolved = setup_commands._resolve_client_env_file(root, False)

    assert resolved == compose_env
    assert not resolved.exists()


@pytest.mark.unit
def test_run_client_setup_blocks_apply_when_env_file_absent(tmp_path: Path) -> None:
    """Regression for PRRT_kwDOSJAM6s6G4DGD: a non-dry-run ``awf setup --client``
    against a not-yet-bootstrapped checkout blocks with START_COMPOSE_ASSETS_MISSING
    instead of reporting success for an MCP server that cannot start."""
    root = _make_source_checkout(tmp_path / "awf")

    payload = setup_commands._run_client_setup(
        clients=["claude"],
        dry_run=False,
        source_checkout=root,
    )

    assert payload.status == "blocked"
    assert payload.reason_code == START_COMPOSE_ASSETS_MISSING
    detail = payload.issues[0].details
    assert detail["check"] == "client_env_file"
    assert detail["env_file"].endswith("docker/compose/.env")
