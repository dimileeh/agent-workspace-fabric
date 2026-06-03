"""CLI coverage for the ``awf setup`` T08 client integration dispatch."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import click
import pytest

from awf.cli import setup_commands
from awf.cli.main import app
from awf.host_setup.config import HostSetupConfig, HostSetupConfigError
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_INVALID,
    SourceCheckoutAssetMetadata,
    SourceCheckoutError,
    validate_source_checkout,
)
from tests.unit.cli._setup_commands_shared import (
    _CLIENT_ENV_FILE,
    _ClientHarness,
    _Harness,
    _make_source_checkout,
    _runner,
    _which_found,
)

# --- T08: client integration dispatch -------------------------------------


@pytest.mark.unit
def test_setup_help_describes_client_option() -> None:
    """Verify setup help advertises the --client integration selector."""
    result = _runner.invoke(app, ["setup", "--help"], env={"COLUMNS": "200"})
    visible_help = click.unstyle(result.output)

    assert result.exit_code == 0, result.output
    assert "--client" in visible_help


@pytest.mark.unit
@pytest.mark.parametrize("client", ["claude", "codex"])
def test_setup_client_dry_run_emits_diff_without_writing(
    client_harness: _ClientHarness, client: str
) -> None:
    """Verify --client X --dry-run returns a diff payload and writes nothing."""
    result = _runner.invoke(app, ["setup", "--client", client, "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["details"]["action"] == "create"
    assert _CLIENT_ENV_FILE in payload["details"]["diff"]
    # No client config file or backup was created under the temp home.
    assert not list(client_harness.home.rglob("*.json"))
    assert not list(client_harness.home.rglob("config.toml"))
    assert not list(client_harness.home.rglob("*.awf-backup-*"))
    assert client_harness.runner_calls == []


@pytest.mark.unit
def test_setup_unknown_client_exits_two(client_harness: _ClientHarness) -> None:
    """Verify an unknown --client renders SETUP_CLIENT_UNKNOWN and exits 2."""
    result = _runner.invoke(app, ["setup", "--client", "emacs", "--dry-run", "--format", "json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "SETUP_CLIENT_UNKNOWN"
    assert payload["issues"][0]["details"]["known_clients"] == ["claude", "codex"]


@pytest.mark.unit
def test_setup_client_with_provider_exits_two_without_writing(
    client_harness: _ClientHarness,
) -> None:
    """Verify --client with --provider fails fast instead of discarding --provider.

    Regression for PRRT_kwDOSJAM6s6GxLDJ: the client dispatch never consumes
    ``--provider``, so a combined ``awf setup --client claude --provider anthropic``
    run must reject the unsupported combination rather than report a successful
    client setup that silently ignored the provider argument.
    """
    result = _runner.invoke(
        app,
        ["setup", "--client", "claude", "--provider", "anthropic", "--format", "json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    # The conflict reports SETUP_PROVIDER_CLIENT_CONFLICT, not
    # SETUP_PROVIDER_UNKNOWN: the provider name may be perfectly valid, so the
    # remediation must describe the mutually exclusive flags rather than imply an
    # unsupported provider (PRRT_kwDOSJAM6s6Gx30C).
    assert payload["reason_code"] == "SETUP_PROVIDER_CLIENT_CONFLICT"
    assert payload["issues"][0]["details"]["providers"] == ["anthropic"]
    assert payload["issues"][0]["details"]["clients"] == ["claude"]
    # The next-step hint points at details keys that actually exist, not the
    # SETUP_PROVIDER_UNKNOWN-only ``known_providers`` key.
    assert payload["next_steps"] == [
        "Re-run awf setup with either --provider or --client, not both; the "
        "rejected selectors are listed under providers and clients in the "
        "issue details.",
    ]
    # No client config was written for the rejected run.
    assert not list(client_harness.home.rglob("*.json"))
    assert not list(client_harness.home.rglob("config.toml"))
    assert client_harness.runner_calls == []


@pytest.mark.unit
def test_setup_client_with_allow_plain_secrets_exits_two_without_writing(
    client_harness: _ClientHarness,
) -> None:
    """Verify --client with --allow-plain-secrets rejects instead of dropping consent.

    Regression for PRRT_kwDOSJAM6s6G04Qk: the client dispatch never reaches
    ``_run_setup`` (the only path that persists the plain-file consent), so a
    combined ``awf setup --client claude --allow-plain-secrets`` run must reject
    the unsupported combination rather than report a successful client setup that
    silently dropped the operator's opt-in flag — which would leave later
    credential setup still blocked despite the flag having been accepted.
    """
    result = _runner.invoke(
        app,
        ["setup", "--client", "claude", "--allow-plain-secrets", "--format", "json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "SETUP_PLAIN_SECRETS_CLIENT_CONFLICT"
    assert payload["issues"][0]["details"]["clients"] == ["claude"]
    assert payload["next_steps"] == [
        "Re-run awf setup --client without --allow-plain-secrets; plain-file "
        "consent is recorded by the readiness/provider path (awf setup), not the "
        "client path.",
    ]
    # No client config was written for the rejected run.
    assert not list(client_harness.home.rglob("*.json"))
    assert not list(client_harness.home.rglob("config.toml"))
    assert client_harness.runner_calls == []


@pytest.mark.unit
def test_setup_client_with_allow_plain_secrets_dry_run_exits_two(
    client_harness: _ClientHarness,
) -> None:
    """Verify the consent rejection fires even on a dry-run client invocation.

    The flag is dropped on any ``--client`` invocation, so the guard rejects it
    regardless of ``--dry-run`` rather than appearing to accept a consent the
    dry-run client path never records.
    """
    result = _runner.invoke(
        app,
        ["setup", "--client", "claude", "--allow-plain-secrets", "--dry-run", "--format", "json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "SETUP_PLAIN_SECRETS_CLIENT_CONFLICT"
    assert not list(client_harness.home.rglob("*.json"))
    assert not list(client_harness.home.rglob("config.toml"))


@pytest.mark.unit
def test_setup_client_apply_writes_config_and_backup(client_harness: _ClientHarness) -> None:
    """Verify a non-dry-run --client update writes config and a backup."""
    config_path = client_harness.home / ".claude.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"type": "stdio", "command": "x", "args": []}}}),
        encoding="utf-8",
    )

    result = _runner.invoke(app, ["setup", "--client", "claude", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["details"]["wrote"] is True
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert "awf" in written["mcpServers"]
    assert written["mcpServers"]["other"]["command"] == "x"
    backups = list(client_harness.home.glob(".claude.json.awf-backup-*"))
    assert len(backups) == 1


@pytest.mark.unit
def test_setup_client_conflict_exits_nonzero_without_mutation(
    client_harness: _ClientHarness,
) -> None:
    """Verify a conflicting client config blocks with no mutation."""
    config_path = client_harness.home / ".claude.json"
    original = json.dumps({"mcpServers": {"awf": {"command": "other", "args": []}}})
    config_path.write_text(original, encoding="utf-8")

    result = _runner.invoke(app, ["setup", "--client", "claude", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "CLIENT_CONFIG_CONFLICT"
    assert config_path.read_text(encoding="utf-8") == original
    assert not list(client_harness.home.glob(".claude.json.awf-backup-*"))


@pytest.mark.unit
def test_setup_client_official_cli_path_invokes_cli_without_file_write(
    client_harness: _ClientHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a resolvable client CLI is invoked and no config file is written."""
    monkeypatch.setattr(setup_commands, "_client_which", _which_found)

    result = _runner.invoke(app, ["setup", "--client", "claude", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert len(client_harness.runner_calls) == 1
    assert client_harness.runner_calls[0][0] == "claude"
    assert client_harness.runner_calls[0][-1] == _CLIENT_ENV_FILE
    assert not list(client_harness.home.rglob("*.json"))


@pytest.mark.unit
def test_setup_repeated_clients_combine_into_one_report(
    client_harness: _ClientHarness,
) -> None:
    """Verify repeated --client selectors produce one combined client report."""
    result = _runner.invoke(
        app,
        ["setup", "--client", "claude", "--client", "codex", "--dry-run", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert set(payload["details"]["clients"]) == {"claude", "codex"}
    assert (client_harness.home / ".claude.json").exists() is False
    assert (client_harness.home / ".codex" / "config.toml").exists() is False


@pytest.mark.unit
def test_setup_no_client_path_unchanged(harness: _Harness) -> None:
    """Regression: the no `--client` path still runs the readiness pass."""
    result = _runner.invoke(app, ["setup", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "awf setup"
    assert payload["details"]["dry_run"] is True
    # The readiness payload reports checks, not a client integration report.
    assert "clients" not in payload["details"]


@pytest.mark.unit
def test_setup_mixed_clients_block_when_one_conflicts(client_harness: _ClientHarness) -> None:
    """Verify a combined report blocks when one of several clients conflicts."""
    # Claude has a conflicting awf entry; codex has no config (a clean create).
    (client_harness.home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"awf": {"command": "other", "args": []}}}),
        encoding="utf-8",
    )

    result = _runner.invoke(
        app,
        ["setup", "--client", "claude", "--client", "codex", "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "CLIENT_CONFIG_CONFLICT"
    assert set(payload["details"]["clients"]) == {"claude", "codex"}
    # Regression (PRRT_kwDOSJAM6s6GxKEf): the clean codex create must NOT be
    # applied when a sibling client conflicts -- no partial write.
    assert not (client_harness.home / ".codex" / "config.toml").exists()


@pytest.mark.unit
def test_setup_mixed_clients_no_partial_write_when_later_client_conflicts(
    client_harness: _ClientHarness,
) -> None:
    """Regression (PRRT_kwDOSJAM6s6GxKEf): a conflict in a later-listed client
    must not leave an earlier, non-conflicting client partially written."""
    # Claude has no config (a clean create, listed first); codex has a
    # conflicting awf entry (listed second).
    codex_config = client_harness.home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    original_codex = '[mcp_servers.awf]\ncommand = "other"\nargs = []\n'
    codex_config.write_text(original_codex, encoding="utf-8")

    result = _runner.invoke(
        app,
        ["setup", "--client", "claude", "--client", "codex", "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "CLIENT_CONFIG_CONFLICT"
    # The earlier, clean claude client must not have been written.
    assert not (client_harness.home / ".claude.json").exists()
    # The conflicting codex config is left exactly as it was.
    assert codex_config.read_text(encoding="utf-8") == original_codex


@pytest.mark.unit
def test_setup_client_invalid_source_checkout_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression for PRRT_kwDOSJAM6s6GxEYa: an explicit, invalid ``--source-checkout``
    on the ``--client`` path blocks with SOURCE_CHECKOUT_INVALID instead of emitting a
    client config diff/write pointing the MCP server at a non-checkout env file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup_commands, "_client_home", lambda: home)
    empty = tmp_path / "empty-checkout"
    empty.mkdir()

    result = _runner.invoke(
        app,
        [
            "setup",
            "--client",
            "claude",
            "--source-checkout",
            str(empty),
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == SOURCE_CHECKOUT_INVALID
    # No client config report/diff was produced for the unvalidated path.
    assert "clients" not in payload.get("details", {})
    assert not list(home.rglob("*.json"))


@pytest.mark.unit
def test_resolve_client_env_file_uses_source_checkout_when_given(tmp_path: Path) -> None:
    """Verify an explicit, valid source checkout pins its docker/compose/.env path."""
    root = _make_source_checkout(tmp_path / "awf")
    resolved = setup_commands._resolve_client_env_file(root)

    assert resolved == validate_source_checkout(root).root / "docker" / "compose" / ".env"


@pytest.mark.unit
def test_resolve_client_env_file_explicit_checkout_falls_back_to_root_env(
    tmp_path: Path,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6G0gfq: a not-yet-bootstrapped source checkout
    can lack ``docker/compose/.env`` while the checkout root ``.env`` exists. ``awf
    start`` reads that root fallback via ``_resolve_start_bootstrap_inputs``; the
    MCP client ``--env-file`` must do the same instead of pinning the absent
    compose path, which would make ``awf mcp serve`` reject the env file."""
    root = _make_source_checkout(tmp_path / "awf")
    root_env = root / ".env"
    root_env.write_text("AWF_API_TOKEN=x\n", encoding="utf-8")
    assert not (root / "docker" / "compose" / ".env").exists()

    resolved = setup_commands._resolve_client_env_file(root)

    assert resolved == root_env


@pytest.mark.unit
def test_resolve_client_env_file_persisted_checkout_falls_back_to_root_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression for PRRT_kwDOSJAM6s6G0gfq: the persisted-checkout branch applies
    the same root ``.env`` fallback as the explicit branch and ``awf start`` so a
    first-run persisted checkout (compose ``.env`` absent, root ``.env`` present)
    registers an MCP ``--env-file`` ``awf mcp serve`` can actually read."""
    root = _make_source_checkout(tmp_path / "awf")
    root_env = root / ".env"
    root_env.write_text("AWF_API_TOKEN=x\n", encoding="utf-8")
    persisted = HostSetupConfig(source_checkout=validate_source_checkout(root).to_metadata())
    monkeypatch.setattr(setup_commands, "read_host_setup_config", lambda **_kw: persisted)

    resolved = setup_commands._resolve_client_env_file(None)

    assert resolved == root_env


@pytest.mark.unit
def test_resolve_client_env_file_invalid_source_checkout_raises(tmp_path: Path) -> None:
    """Regression for PRRT_kwDOSJAM6s6GxEYa: an explicit ``--source-checkout`` is
    validated like the readiness flow, so an invalid path raises instead of pinning
    an MCP ``--env-file`` under a non-checkout directory."""
    empty = tmp_path / "empty-checkout"
    empty.mkdir()

    with pytest.raises(SourceCheckoutError):
        setup_commands._resolve_client_env_file(empty)


@pytest.mark.unit
def test_resolve_client_env_file_defaults_to_compose_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the default path comes from resolve_service_compose_paths.

    With no explicit ``--source-checkout`` and no persisted source metadata, the
    client env file falls back to the packaged/default ``.env``.
    """
    monkeypatch.setattr(setup_commands, "read_host_setup_config", lambda **_kw: HostSetupConfig())
    monkeypatch.setattr(
        setup_commands,
        "resolve_service_compose_paths",
        lambda: (Path("/x/compose.yml"), Path("/x/.env"), Path("/x/.env.example")),
    )

    assert setup_commands._resolve_client_env_file(None) == Path("/x/.env")


@pytest.mark.unit
def test_resolve_client_env_file_fallback_is_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6GyLKV: the packaged/default fallback may be a
    relative ``Path(".env")``, but ``awf mcp serve`` resolves the registered
    ``--env-file`` against the client process cwd. Persist an absolute path so
    Claude/Codex sessions launched from any directory still find the env file."""
    monkeypatch.setattr(setup_commands, "read_host_setup_config", lambda **_kw: HostSetupConfig())
    monkeypatch.setattr(
        setup_commands,
        "resolve_service_compose_paths",
        lambda: (Path("compose.yml"), Path(".env"), Path(".env.example")),
    )

    resolved = setup_commands._resolve_client_env_file(None)

    assert resolved.is_absolute()
    assert resolved == Path(".env").resolve()


@pytest.mark.unit
def test_resolve_client_env_file_honors_persisted_source_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify a persisted, still-valid checkout pins its docker/compose/.env.

    Regression for PRRT_kwDOSJAM6s6GxEX4: ``awf setup --client`` without
    ``--source-checkout`` must honor source-checkout metadata stored by an earlier
    run — the same checkout ``awf start``'s ``_resolve_start_source_checkout``
    revalidates — instead of always defaulting to the packaged ``.env`` and
    registering an MCP ``--env-file`` that diverges from the env ``awf start`` uses.
    """
    root = _make_source_checkout(tmp_path / "awf")
    persisted = HostSetupConfig(source_checkout=validate_source_checkout(root).to_metadata())
    monkeypatch.setattr(setup_commands, "read_host_setup_config", lambda **_kw: persisted)

    resolved = setup_commands._resolve_client_env_file(None)

    assert resolved == validate_source_checkout(root).root / "docker" / "compose" / ".env"


@pytest.mark.unit
def test_resolve_client_env_file_stale_persisted_falls_back_to_compose_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify stale persisted metadata falls back to default discovery.

    A moved/invalid persisted checkout (the same failure ``awf start`` would
    reject) must not pin a dead path into the MCP client config; the resolver
    falls back to the packaged/default ``.env`` instead.
    """
    stale = HostSetupConfig(
        source_checkout=SourceCheckoutAssetMetadata(
            root=tmp_path / "gone", verified_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    monkeypatch.setattr(setup_commands, "read_host_setup_config", lambda **_kw: stale)
    monkeypatch.setattr(
        setup_commands,
        "resolve_service_compose_paths",
        lambda: (Path("/x/compose.yml"), Path("/x/.env"), Path("/x/.env.example")),
    )

    assert setup_commands._resolve_client_env_file(None) == Path("/x/.env")


@pytest.mark.unit
def test_resolve_client_env_file_unreadable_config_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify an unreadable host config falls back to default discovery.

    A corrupt ``~/.awf/config.yml`` must not crash the client env resolution; like
    ``awf start``'s resolver it falls back to default compose paths.
    """

    def _raise(**_kw: object) -> HostSetupConfig:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_CORRUPT",
            message="corrupt",
            path=Path("/x/config.yml"),
        )

    monkeypatch.setattr(setup_commands, "read_host_setup_config", _raise)
    monkeypatch.setattr(
        setup_commands,
        "resolve_service_compose_paths",
        lambda: (Path("/x/compose.yml"), Path("/x/.env"), Path("/x/.env.example")),
    )

    assert setup_commands._resolve_client_env_file(None) == Path("/x/.env")


@pytest.mark.unit
def test_client_seams_resolve_real_home_and_clock() -> None:
    """Verify the production client seams resolve the host home and UTC clock."""
    assert setup_commands._client_home() == Path.home()
    now = setup_commands._client_now()
    assert now.tzinfo is UTC
    assert setup_commands._client_env() is os.environ


@pytest.mark.unit
def test_setup_client_codex_honors_codex_home(
    client_harness: _ClientHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the CLI threads CODEX_HOME so Codex config writes under it.

    Regression for PRRT_kwDOSJAM6s6GyZ1U: with CODEX_HOME set, the file-fallback
    apply must target ``$CODEX_HOME/config.toml`` (where the Codex CLI reads),
    not the hard-coded ``~/.codex/config.toml``.
    """
    codex_home = client_harness.home / "xdg-codex"
    monkeypatch.setattr(setup_commands, "_client_env", lambda: {"CODEX_HOME": str(codex_home)})

    result = _runner.invoke(app, ["setup", "--client", "codex", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["details"]["config_path"] == str(codex_home / "config.toml")
    assert (codex_home / "config.toml").exists()
    assert not (client_harness.home / ".codex" / "config.toml").exists()
