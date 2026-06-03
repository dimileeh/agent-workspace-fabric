"""CLI coverage for the ``awf setup`` read-only host readiness pass."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import click
import pytest

from awf.cli import setup_commands
from awf.cli.main import app
from awf.host_setup.config import HostSetupConfig, HostSetupConfigError, ProviderConfig
from awf.host_setup.providers import (
    ProviderSetupResult,
    ProviderSetupSummary,
)
from awf.host_setup.rendering import (
    INTERACTIVE_INPUT_REQUIRED,
    SETUP_READINESS_FAILED,
    START_COMPOSE_ASSETS_MISSING,
)
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_INVALID,
    SourceCheckoutAssetMetadata,
    validate_source_checkout,
)
from awf.host_setup.system_checks import SetupCheckLevel, SetupCheckResult
from tests.unit.cli._setup_commands_harness import (
    _all_ok,
    _docker_blocked,
    _gh_warning,
    _Harness,
    _make_source_checkout,
    _real_readiness_environ,
    _runner,
    harness,
)

__all__ = ["harness"]


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


@pytest.mark.unit
def test_setup_success_next_step_is_provider_free_and_leads_to_start(
    harness: _Harness,
) -> None:
    """The setup→start→smoke first-run chain stays provider-free (T10).

    Setup's success next step must point at ``awf start`` (no token/provider
    prompt), guarding the documented first-run chain into the provider-free local
    proof without duplicating T07 provider logic.
    """
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    next_steps = json.loads(result.stdout)["next_steps"]
    leading = next_steps[0].lower()
    assert "awf start" in leading
    assert "token" not in leading
    assert "secret" not in leading
    assert "provider" not in leading


# --- Compose env merge for host checks ------------------------------------


@pytest.mark.unit
def test_setup_merges_compose_env_when_probing_api_port(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host checks receive the merged Compose env so an ``AWF_API_HOST_PORT``
    set only in ``docker/compose/.env`` is honored, not just the process env."""
    import awf.service.config as service_config

    merged = {"AWF_API_HOST_PORT": "9100"}
    monkeypatch.setattr(service_config, "local_service_environ", lambda *_a, **_kw: merged)
    # The harness stubs _readiness_environ for hermetic isolation; this test
    # proves the real resolver forwards the merged compose env to the checks.
    monkeypatch.setattr(setup_commands, "_readiness_environ", _real_readiness_environ)

    captured: dict[str, object] = {}

    def fake_checks(**kwargs: object) -> list[SetupCheckResult]:
        captured["environ"] = kwargs.get("environ")
        return _all_ok()

    monkeypatch.setattr(setup_commands, "run_system_checks", fake_checks)
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert captured["environ"] == merged


@pytest.mark.unit
def test_setup_probes_selected_source_checkout_env(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The readiness probes read the *selected* checkout's ``docker/compose/.env``.

    Regression for PRRT_kwDOSJAM6s6F6Tt0: ``awf setup --source-checkout
    /other/awf`` must resolve ``AWF_API_HOST_PORT``/``AWF_HOST_WORK_DIR`` from the
    selected checkout's ``docker/compose/.env`` — the same file ``awf start``
    later honors for the persisted checkout — instead of default ``.env``
    discovery, so setup never probes/blocks on a port or disk path the matching
    ``awf start`` would not use.
    """
    root = _make_source_checkout(tmp_path / "awf")
    compose_env = root / "docker" / "compose" / ".env"
    compose_env.parent.mkdir(parents=True, exist_ok=True)
    compose_env.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")
    # The harness stubs _readiness_environ; this test exercises the real resolver.
    monkeypatch.setattr(setup_commands, "_readiness_environ", _real_readiness_environ)

    captured: dict[str, object] = {}

    def fake_checks(**kwargs: object) -> list[SetupCheckResult]:
        captured["environ"] = kwargs.get("environ")
        return _all_ok()

    monkeypatch.setattr(setup_commands, "run_system_checks", fake_checks)
    result = _runner.invoke(
        app,
        ["setup", "--dry-run", "--source-checkout", str(root), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    environ = captured["environ"]
    assert isinstance(environ, dict)
    assert environ["AWF_API_HOST_PORT"] == "9100"


@pytest.mark.unit
def test_setup_probes_persisted_source_checkout_env(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``--source-checkout``: the probe honors the *persisted* checkout's env.

    Regression for PRRT_kwDOSJAM6s6F6Wjh: after a prior run stores source-checkout
    metadata in host config, ``awf setup`` without ``--source-checkout`` must
    resolve ``AWF_API_HOST_PORT``/``AWF_HOST_WORK_DIR`` from that persisted
    checkout's ``docker/compose/.env`` — the file ``awf start`` revalidates and
    honors via ``_resolve_start_source_checkout`` — instead of default discovery,
    so setup never probes/clears on a port or disk path the matching ``awf start``
    would not use.
    """
    root = _make_source_checkout(tmp_path / "awf")
    compose_env = root / "docker" / "compose" / ".env"
    compose_env.parent.mkdir(parents=True, exist_ok=True)
    compose_env.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")

    persisted = HostSetupConfig(source_checkout=validate_source_checkout(root).to_metadata())
    monkeypatch.setattr(setup_commands, "read_host_setup_config", lambda **_kw: persisted)
    # The harness stubs _readiness_environ; this test exercises the real resolver.
    monkeypatch.setattr(setup_commands, "_readiness_environ", _real_readiness_environ)

    captured: dict[str, object] = {}

    def fake_checks(**kwargs: object) -> list[SetupCheckResult]:
        captured["environ"] = kwargs.get("environ")
        return _all_ok()

    monkeypatch.setattr(setup_commands, "run_system_checks", fake_checks)
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    environ = captured["environ"]
    assert isinstance(environ, dict)
    assert environ["AWF_API_HOST_PORT"] == "9100"


@pytest.mark.unit
def test_setup_no_checkout_probes_bootstrap_asset_root_env(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No source checkout: the probe honors the bootstrap asset root's compose env.

    Regression for PRRT_kwDOSJAM6s6F6ljB: with no verified source checkout in
    play, ``awf setup`` must resolve ``AWF_API_HOST_PORT``/``AWF_HOST_WORK_DIR``
    the same way ``awf start`` does — through ``_resolve_service_compose_paths``/
    ``_resolve_service_runtime_env_files``, which honor the packaged bootstrap
    asset root's ``docker/compose/.env`` — instead of bare default discovery that
    only searches the cwd and nearby source markers. Otherwise setup probes the
    default 8000/work dir while start uses the bundled env file's values.
    """
    asset_root = tmp_path / "assets"
    compose_dir = asset_root / "docker" / "compose"
    compose_dir.mkdir(parents=True, exist_ok=True)
    (compose_dir / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose_dir / ".env").write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")

    import awf.service.bootstrap as bootstrap_mod

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: asset_root)
    monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)
    # The harness stubs _readiness_environ; this test exercises the real resolver.
    monkeypatch.setattr(setup_commands, "_readiness_environ", _real_readiness_environ)

    captured: dict[str, object] = {}

    def fake_checks(**kwargs: object) -> list[SetupCheckResult]:
        captured["environ"] = kwargs.get("environ")
        return _all_ok()

    monkeypatch.setattr(setup_commands, "run_system_checks", fake_checks)
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    environ = captured["environ"]
    assert isinstance(environ, dict)
    assert environ["AWF_API_HOST_PORT"] == "9100"


@pytest.mark.unit
def test_setup_no_checkout_missing_bootstrap_assets_blocks(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default discovery with no resolvable bootstrap assets blocks like ``awf start``.

    Regression for PRRT_kwDOSJAM6s6F-MZF: outside a source checkout and without
    bundled bootstrap assets, ``get_bootstrap_asset_root()`` is ``None`` and
    ``awf start`` later fails in ``run_service_bootstrap`` with
    SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND (START_COMPOSE_ASSETS_MISSING). ``awf
    setup`` must surface the same blocker instead of reporting a ready host and
    telling the operator to run a start that cannot resolve its compose/runtime
    assets.
    """
    import awf.service.bootstrap as bootstrap_mod

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    reason_codes = [issue["reason_code"] for issue in payload["issues"]]
    assert START_COMPOSE_ASSETS_MISSING in reason_codes


@pytest.mark.unit
def test_setup_source_checkout_skips_bootstrap_assets_blocker(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A verified source checkout never trips the default-discovery assets blocker.

    The bootstrap-assets readiness blocker only guards the default-discovery path:
    a selected ``--source-checkout`` already pins a valid asset root that ``awf
    start`` reuses, so setup must not block on a ``None``
    ``get_bootstrap_asset_root()`` that only applies when no checkout is selected.
    """
    import awf.service.bootstrap as bootstrap_mod

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    root = _make_source_checkout(tmp_path / "awf")
    result = _runner.invoke(
        app,
        ["setup", "--dry-run", "--source-checkout", str(root), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    reason_codes = [issue["reason_code"] for issue in payload.get("issues", [])]
    assert START_COMPOSE_ASSETS_MISSING not in reason_codes


@pytest.mark.unit
def test_setup_persisted_source_checkout_stale_blocks(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``--source-checkout``: stale persisted metadata blocks like ``awf start``.

    When a stored checkout has moved or lost required assets, ``awf start``
    revalidates it and fails with SOURCE_CHECKOUT_ASSETS_STALE rather than
    silently falling back to package discovery. ``awf setup`` must surface the
    same blocker instead of probing default discovery and reporting ready, so the
    readiness pass predicts the start failure rather than masking it.
    """
    persisted = HostSetupConfig(
        source_checkout=SourceCheckoutAssetMetadata(
            root=tmp_path / "gone",
            verified_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
    )
    monkeypatch.setattr(setup_commands, "read_host_setup_config", lambda **_kw: persisted)
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    reason_codes = [issue["reason_code"] for issue in payload["issues"]]
    assert "SOURCE_CHECKOUT_ASSETS_STALE" in reason_codes


@pytest.mark.unit
def test_setup_no_flag_preserves_persisted_source_metadata(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A no-flag non-dry-run run preserves persisted source metadata untouched.

    Only an explicit ``--source-checkout`` selection writes/refreshes source
    metadata; re-running ``awf setup`` without the flag must round-trip the
    persisted metadata (and its ``verified_at``) unchanged rather than silently
    rewriting it.
    """
    root = _make_source_checkout(tmp_path / "awf")
    metadata = validate_source_checkout(root).to_metadata()
    persisted = HostSetupConfig(source_checkout=metadata)
    monkeypatch.setattr(setup_commands, "read_host_setup_config", lambda **_kw: persisted)

    result = _runner.invoke(app, ["setup", "--format", "json"])

    assert result.exit_code == 0, result.output
    written = harness.writes[-1]
    assert written.source_checkout == metadata


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


@pytest.mark.unit
def test_setup_unknown_provider_payload_carries_next_steps(harness: _Harness) -> None:
    """Verify the SETUP_PROVIDER_UNKNOWN error payload carries next-step guidance.

    Regression for review comment issue:4585200251: the reason-coded error exits
    built by ``_reason_coded_payload`` left top-level ``next_steps`` empty, unlike
    the happy path, so a script hitting an unknown provider got no machine-readable
    pointer to the accepted provider names recorded in the issue details.
    """
    result = _runner.invoke(app, ["setup", "--provider", "bogus", "--dry-run", "--format", "json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "SETUP_PROVIDER_UNKNOWN"
    assert payload["next_steps"]


@pytest.mark.unit
def test_setup_interactive_required_payload_carries_next_steps(harness: _Harness) -> None:
    """Verify the INTERACTIVE_INPUT_REQUIRED error payload carries next-step guidance.

    Regression for review comment issue:4585200251: a scripted
    ``--provider X --non-interactive`` run that trips the interactive guard now
    receives a top-level pointer to re-run without ``--non-interactive`` (or with
    ``--dry-run``) instead of the silent empty ``next_steps`` it returned before.
    """
    result = _runner.invoke(
        app,
        ["setup", "--provider", "github", "--non-interactive", "--format", "json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "INTERACTIVE_INPUT_REQUIRED"
    assert payload["next_steps"]


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


@pytest.mark.unit
def test_setup_invalid_source_checkout_skips_default_discovery_probes(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed checkout validation surfaces the error without default-discovery probes.

    Regression for PRRT_kwDOSJAM6s6F7ys0: ``awf start`` exits from
    ``_resolve_start_source_checkout`` before it reaches
    ``_resolve_start_bootstrap_inputs``/default discovery for an invalid
    selection, so ``awf setup`` must mirror that. When the source checkout fails
    validation it must NOT call ``run_system_checks`` against the
    default-discovered compose env, so it cannot add unrelated default port/disk
    blockers (for example the default 8000 in use) the matching ``awf start``
    would never hit. Only the source-checkout error remains.
    """
    # Exercise the real env resolver so a regression that reintroduces the probe
    # also reintroduces its default-discovery IO, not just the call.
    monkeypatch.setattr(setup_commands, "_readiness_environ", _real_readiness_environ)

    bad_root = tmp_path / "not-awf"
    bad_root.mkdir()

    probe_calls: list[object] = []

    def fake_checks(**kwargs: object) -> list[SetupCheckResult]:
        probe_calls.append(kwargs.get("environ"))
        # A default-discovery probe would report this port blocker; it must never
        # reach the payload when the checkout itself failed validation.
        return [
            SetupCheckResult(
                name="ports",
                level=SetupCheckLevel.BLOCKED,
                summary="default API host port 8000 is already in use",
                detail="0.0.0.0:8000 already bound",
                fix="free the port",
            )
        ]

    monkeypatch.setattr(setup_commands, "run_system_checks", fake_checks)
    result = _runner.invoke(
        app,
        ["setup", "--dry-run", "--source-checkout", str(bad_root), "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    # The default-discovery host probes never ran, so no unrelated port/disk
    # blocker leaks into the payload; only the source-checkout error remains.
    assert probe_calls == []
    reason_codes = [issue["reason_code"] for issue in payload["issues"]]
    assert reason_codes == [SOURCE_CHECKOUT_INVALID]
    assert SETUP_READINESS_FAILED not in reason_codes


@pytest.mark.unit
def test_setup_source_checkout_failure_persists_plain_secret_consent(
    harness: _Harness, tmp_path: Path
) -> None:
    """Verify explicit --allow-plain-secrets survives a source-checkout failure.

    Regression for review comment issue:4585200251: a non-dry-run
    ``--source-checkout <bad> --allow-plain-secrets`` run blocks on the invalid
    checkout, yet the explicit, non-secret plain-file consent must still be
    persisted -- mirroring how a host-check blocker persists it -- so an operator
    need not re-pass ``--allow-plain-secrets`` after fixing the checkout path. The
    failed checkout itself is never recorded.
    """
    bad_root = tmp_path / "not-awf"
    bad_root.mkdir()
    result = _runner.invoke(
        app,
        [
            "setup",
            "--source-checkout",
            str(bad_root),
            "--allow-plain-secrets",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    reason_codes = [issue["reason_code"] for issue in payload["issues"]]
    assert SOURCE_CHECKOUT_INVALID in reason_codes
    written = harness.writes[-1]
    assert written.consent.plain_file_secrets is True
    # The invalid checkout is never persisted, and its consent flag stays false.
    assert written.source_checkout is None
    assert written.consent.source_checkout_assets is False


@pytest.mark.unit
def test_setup_source_checkout_failure_folds_consent_write_failure(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify a consent-write failure on the blocked source path is folded, not raised.

    Companion to the consent-persistence regression: when persisting the explicit
    ``--allow-plain-secrets`` consent fails on the blocked source-checkout early
    return, the write failure must surface alongside the source-checkout blocker
    rather than replacing it or raising a traceback.
    """

    def raise_write_failed(_config: HostSetupConfig, **_kw: object) -> None:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_WRITE_FAILED",
            message="Unable to write host setup config.",
            path=Path("/tmp/.awf/config.yml"),
            details={"error_type": "PermissionError"},
        )

    monkeypatch.setattr(setup_commands, "write_host_setup_config", raise_write_failed)
    bad_root = tmp_path / "not-awf"
    bad_root.mkdir()
    result = _runner.invoke(
        app,
        [
            "setup",
            "--source-checkout",
            str(bad_root),
            "--allow-plain-secrets",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    reason_codes = [issue["reason_code"] for issue in payload["issues"]]
    assert SOURCE_CHECKOUT_INVALID in reason_codes
    assert "HOST_SETUP_CONFIG_WRITE_FAILED" in reason_codes
    assert "Traceback" not in result.stdout


@pytest.mark.unit
def test_setup_source_checkout_failure_skips_redundant_consent_write(
    harness: _Harness, tmp_path: Path
) -> None:
    """Verify a blocked source checkout with nothing to record writes no config.

    Regression for review comment issue:4585200251: a non-dry-run
    ``--source-checkout <bad>`` run with no ``--allow-plain-secrets`` (and no
    plain-file consent already on disk) has nothing safe to persist on the
    blocked early return -- the failed checkout is never recorded and the only
    other field is the plain-file flag -- so it must not create or rewrite the
    host config file for an identical, no-op write.
    """
    bad_root = tmp_path / "not-awf"
    bad_root.mkdir()
    result = _runner.invoke(
        app,
        ["setup", "--source-checkout", str(bad_root), "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    reason_codes = [issue["reason_code"] for issue in payload["issues"]]
    assert SOURCE_CHECKOUT_INVALID in reason_codes
    # No consent to record, so the blocked early return leaves the config untouched.
    assert harness.writes == []


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
def test_setup_non_interactive_provider_persists_plain_secret_consent(harness: _Harness) -> None:
    """Verify explicit --allow-plain-secrets survives the provider input guard.

    Regression for review comment issue:4585200251: a ready-host
    ``--provider X --non-interactive --allow-plain-secrets`` run still aborts
    with INTERACTIVE_INPUT_REQUIRED (provider credentials cannot be collected),
    but the explicitly-passed, non-secret plain-file consent must be persisted
    rather than silently dropped before the guard fires.
    """
    result = _runner.invoke(
        app,
        [
            "setup",
            "--provider",
            "github",
            "--non-interactive",
            "--allow-plain-secrets",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "INTERACTIVE_INPUT_REQUIRED"
    assert harness.writes[-1].consent.plain_file_secrets is True


@pytest.mark.unit
def test_setup_non_interactive_provider_persists_source_checkout(
    harness: _Harness, tmp_path: Path
) -> None:
    """Verify an explicit --source-checkout survives the provider input guard.

    Companion to the plain-secret regression: the ``--source-checkout`` selection
    is an explicit CLI flag, not interactive input, so its verified metadata and
    consent must be recorded even when the non-interactive provider guard aborts.
    """
    root = _make_source_checkout(tmp_path / "awf")
    result = _runner.invoke(
        app,
        [
            "setup",
            "--provider",
            "github",
            "--non-interactive",
            "--source-checkout",
            str(root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "INTERACTIVE_INPUT_REQUIRED"
    written = harness.writes[-1]
    assert written.source_checkout is not None
    assert written.consent.source_checkout_assets is True


@pytest.mark.unit
def test_setup_non_interactive_persists_resolved_refs_before_guard(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe provider refs survive the interactive guard without explicit consent.

    Regression for review comment 4419120368: a ``--provider github --provider
    codex --non-interactive`` run where GitHub resolves a safe env ref but codex
    still needs an uncollectable secret must persist GitHub's resolved ref before
    raising INTERACTIVE_INPUT_REQUIRED. Previously the guard aborted ahead of the
    write (no explicit consent flags), silently dropping the safe ref.
    """

    def _github_ref_codex_interactive(
        _settings: object,
        *,
        selected_providers: list[str],
        config: HostSetupConfig,
        **_kwargs: object,
    ) -> tuple[ProviderSetupSummary, HostSetupConfig]:
        updated = config.model_copy(
            update={
                "providers": {
                    **dict(config.providers),
                    "github": ProviderConfig(backend="env_ref", credential_ref="env://GH_TOKEN"),
                }
            }
        )
        summary = ProviderSetupSummary(
            mode="targeted_recheck",
            selected=tuple(selected_providers),
            providers=(
                ProviderSetupResult(
                    name="github",
                    status="ready",
                    reason_code="GITHUB_ENV_REF_OK",
                    summary="GitHub configured from env ref.",
                    backend="env_ref",
                    credential_ref="env://GH_TOKEN",
                    configured=True,
                    rechecked=True,
                ),
                ProviderSetupResult(
                    name="codex",
                    status="not_configured",
                    reason_code=INTERACTIVE_INPUT_REQUIRED,
                    summary="codex needs interactive credential entry.",
                ),
            ),
            overall_status="not_ready",
        )
        return summary, updated

    monkeypatch.setattr(setup_commands, "orchestrate_provider_setup", _github_ref_codex_interactive)
    result = _runner.invoke(
        app,
        [
            "setup",
            "--provider",
            "github",
            "--provider",
            "codex",
            "--non-interactive",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "INTERACTIVE_INPUT_REQUIRED"
    written = harness.writes[-1]
    assert written.providers["github"].credential_ref == "env://GH_TOKEN"


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


@pytest.mark.unit
def test_setup_non_interactive_provider_surfaces_readiness_warnings(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-blocking host warnings survive the interactive guard (exit 2).

    Regression for review comment issue:4585200251: ``--provider X
    --non-interactive`` on an otherwise-ready host (a ``gh`` warning, low disk,
    below-floor CPU/memory) tripped the interactive guard and emitted only the
    INTERACTIVE_INPUT_REQUIRED error, discarding the readiness warnings a
    scripting operator keying on exit code 2 needs. Both the block and the host
    warnings (with their check provenance) must now be surfaced together.
    """
    monkeypatch.setattr(setup_commands, "run_system_checks", _gh_warning)
    result = _runner.invoke(
        app,
        ["setup", "--provider", "github", "--non-interactive", "--format", "json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "INTERACTIVE_INPUT_REQUIRED"
    reason_codes = [issue["reason_code"] for issue in payload["issues"]]
    assert "INTERACTIVE_INPUT_REQUIRED" in reason_codes
    assert "SETUP_READINESS_FAILED" in reason_codes
    # The host-check provenance is surfaced rather than discarded.
    assert payload["details"]["checks"]


# --- Provider orchestration integration (T07) -----------------------------


def _ready_github_summary(
    _settings: object,
    *,
    selected_providers: list[str],
    config: HostSetupConfig,
    **_kwargs: object,
) -> tuple[ProviderSetupSummary, HostSetupConfig]:
    """Fake orchestration that marks GitHub ready via gh (no token stored).

    The per-provider ``summary`` text embeds a token-shaped value so the
    no-token assertion in ``test_setup_provider_github_pretty_prints_no_token``
    exercises pretty (stderr) rendering rather than passing vacuously against
    secret-free text. ``ProviderSetupSummary.to_details()`` already drops the
    per-provider ``summary`` field, so the token never reaches the JSON payload;
    the value therefore only guards that the pretty renderer redacts known token
    shapes instead of leaking them.
    """
    summary = ProviderSetupSummary(
        mode="targeted_recheck" if selected_providers else "all_providers",
        selected=tuple(selected_providers),
        providers=(
            ProviderSetupResult(
                name="github",
                status="ready",
                reason_code="GITHUB_GH_AUTH_OK",
                summary="GitHub is ready via gh CLI authentication (ghp_should_be_redacted).",
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
