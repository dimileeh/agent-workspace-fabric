"""Behavior, edge, and error coverage for client MCP integration helpers."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from awf.common.audit import REDACTION_MARKER
from awf.host_setup.clients import (
    AWF_MCP_SERVER_KEY,
    CLIENT_DESCRIPTORS,
    KNOWN_SETUP_CLIENTS,
    ClientConfigPlan,
    apply_client_config_plan,
    build_client_config_plan,
    normalize_client,
    normalize_clients,
    setup_client,
    setup_clients,
)
from awf.host_setup.rendering import (
    CLIENT_CONFIG_CONFLICT,
    CLIENT_CONFIG_WRITE_FAILED,
    SETUP_CLIENT_UNKNOWN,
    render_first_run_json,
    render_first_run_pretty,
)
from awf.host_setup.system_checks import CommandResult, SetupCheckError

_FIXED_NOW = datetime(2026, 6, 3, 12, 30, 45, tzinfo=UTC)
_ENV_FILE = "/srv/awf/docker/compose/.env"


def _now() -> datetime:
    return _FIXED_NOW


def _which_missing(_binary: str) -> str | None:
    return None


def _which_found(binary: str) -> str:
    return f"/usr/bin/{binary}"


class _FakeRunner:
    """Capturing fake ``CommandRunner`` for official-CLI assertions."""

    def __init__(self, result: CommandResult | None) -> None:
        self._result = result
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> CommandResult | None:
        self.calls.append(tuple(args))
        return self._result


def _never_run(args: Sequence[str]) -> CommandResult | None:
    raise AssertionError(f"runner must not be invoked, got {tuple(args)!r}")


def _claude_config_path(home: Path) -> Path:
    return home / ".claude.json"


def _codex_config_path(home: Path) -> Path:
    return home / ".codex" / "config.toml"


def _desired_args(env_file: str = _ENV_FILE) -> list[str]:
    return ["mcp", "serve", "--env-file", env_file]


# --- normalize_client(s) --------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("claude", "claude"),
        ("Claude", "claude"),
        ("claude-code", "claude"),
        ("claude_code", "claude"),
        ("anthropic", "claude"),
        ("codex", "codex"),
        ("OpenAI", "codex"),
    ],
)
def test_normalize_client_canonicalizes_aliases(raw: str, expected: str) -> None:
    """Verify known names and aliases canonicalize to a descriptor key."""
    assert normalize_client(raw) == expected


@pytest.mark.unit
def test_normalize_client_rejects_unknown_with_known_clients_detail() -> None:
    """Verify an unknown client raises SETUP_CLIENT_UNKNOWN with known clients."""
    with pytest.raises(SetupCheckError) as excinfo:
        normalize_client("emacs")

    error = excinfo.value
    assert error.reason_code == SETUP_CLIENT_UNKNOWN
    assert error.details["client"] == "emacs"
    assert error.details["known_clients"] == list(KNOWN_SETUP_CLIENTS)


@pytest.mark.unit
def test_normalize_clients_dedupes_preserving_order() -> None:
    """Verify selectors de-duplicate after aliasing while preserving order."""
    assert normalize_clients(["codex", "claude-code", "claude", "openai"]) == ["codex", "claude"]


# --- build_client_config_plan: create -------------------------------------


@pytest.mark.unit
def test_build_plan_claude_missing_file_creates(tmp_path: Path) -> None:
    """Verify a missing Claude config plans a create with an added awf entry."""
    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "create"
    assert plan.method == "file"
    assert plan.backup_path is None
    assert plan.config_path == _claude_config_path(tmp_path)
    assert AWF_MCP_SERVER_KEY in plan.diff
    assert _ENV_FILE in plan.diff
    assert plan.desired_entry == {
        "type": "stdio",
        "command": "awf",
        "args": _desired_args(),
    }


@pytest.mark.unit
def test_build_plan_codex_missing_file_creates(tmp_path: Path) -> None:
    """Verify a missing Codex config plans a create with the awf TOML table."""
    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "create"
    assert plan.backup_path is None
    assert "[mcp_servers.awf]" in plan.diff
    assert plan.desired_entry["startup_timeout_sec"] == 20
    assert plan.desired_entry["tool_timeout_sec"] == 120
    assert plan.desired_entry["args"] == _desired_args()


# --- build_client_config_plan: no_change ----------------------------------


@pytest.mark.unit
def test_build_plan_claude_identical_entry_is_no_change(tmp_path: Path) -> None:
    """Verify an identical Claude awf entry plans no_change with an empty diff."""
    config = {
        "mcpServers": {
            AWF_MCP_SERVER_KEY: {"type": "stdio", "command": "awf", "args": _desired_args()}
        }
    }
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "no_change"
    assert plan.diff == ""
    assert plan.backup_path is None


@pytest.mark.unit
def test_build_plan_codex_matching_command_args_is_no_change(tmp_path: Path) -> None:
    """Verify a Codex entry with matching command/args plans no_change."""
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        "[mcp_servers.awf]\n"
        'command = "awf"\n'
        f'args = ["mcp", "serve", "--env-file", "{_ENV_FILE}"]\n'
        "startup_timeout_sec = 99\n",
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "no_change"
    assert plan.diff == ""


# --- build_client_config_plan: conflict -----------------------------------


@pytest.mark.unit
def test_build_plan_claude_conflicting_entry_is_conflict(tmp_path: Path) -> None:
    """Verify a divergent Claude awf entry plans a conflict with no write."""
    config = {
        "mcpServers": {
            AWF_MCP_SERVER_KEY: {
                "type": "stdio",
                "command": "other-binary",
                "args": ["--serve"],
            }
        }
    }
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert plan.conflict_detail is not None
    assert plan.merged_config is None
    assert plan.backup_path is None


@pytest.mark.unit
def test_build_plan_codex_conflicting_args_is_conflict(tmp_path: Path) -> None:
    """Verify a Codex awf entry pointing elsewhere plans a conflict."""
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        '[mcp_servers.awf]\ncommand = "awf"\nargs = ["mcp", "serve", "--env-file", "/other/.env"]\n',
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert plan.conflict_detail is not None


@pytest.mark.unit
def test_build_plan_claude_scalar_args_is_conflict(tmp_path: Path) -> None:
    """Verify a Claude awf entry whose args is a non-list scalar plans a conflict.

    A table entry with ``"args": null`` is ambiguous: the old ``list(...)``
    comparison would crash on a non-iterable; it must route to the reason-coded
    conflict path instead.
    """
    config = {"mcpServers": {AWF_MCP_SERVER_KEY: {"type": "stdio", "command": "awf", "args": None}}}
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert "different command/args" in (plan.conflict_detail or "")


@pytest.mark.unit
def test_build_plan_codex_scalar_args_is_conflict(tmp_path: Path) -> None:
    """Verify a Codex awf entry whose args is a scalar plans a conflict, not a crash."""
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        '[mcp_servers.awf]\ncommand = "awf"\nargs = 1\n',
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert "different command/args" in (plan.conflict_detail or "")


@pytest.mark.unit
def test_build_plan_malformed_json_is_conflict(tmp_path: Path) -> None:
    """Verify unparseable existing JSON is an ambiguous conflict."""
    _claude_config_path(tmp_path).write_text("{not valid json", encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert "JSON" in (plan.conflict_detail or "")


@pytest.mark.unit
def test_build_plan_malformed_toml_is_conflict(tmp_path: Path) -> None:
    """Verify unparseable existing TOML is an ambiguous conflict."""
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text("this is = = not toml", encoding="utf-8")

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert "TOML" in (plan.conflict_detail or "")


@pytest.mark.unit
def test_build_plan_codex_unrepresentable_existing_is_conflict(tmp_path: Path) -> None:
    """Verify existing TOML the scoped emitter can't round-trip is a conflict."""
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    # A float value is valid TOML but outside the scoped emitter's representable set.
    codex_path.write_text("sampling_rate = 3.14\n", encoding="utf-8")

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert "round-trip" in (plan.conflict_detail or "")


@pytest.mark.unit
def test_build_plan_non_mapping_json_is_conflict(tmp_path: Path) -> None:
    """Verify a JSON document that is not an object is an ambiguous conflict."""
    _claude_config_path(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"


@pytest.mark.unit
def test_build_plan_non_table_servers_value_is_conflict(tmp_path: Path) -> None:
    """Verify a non-table ``mcpServers`` value is refused, not silently erased."""
    _claude_config_path(tmp_path).write_text(
        json.dumps({"mcpServers": "not-a-table"}), encoding="utf-8"
    )

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert plan.conflict_detail is not None
    assert "mcpServers" in (plan.conflict_detail or "")
    assert plan.merged_config is None


@pytest.mark.unit
def test_build_plan_non_table_awf_entry_is_conflict(tmp_path: Path) -> None:
    """Verify a non-table ``awf`` entry is refused, not silently overwritten."""
    config = {"mcpServers": {AWF_MCP_SERVER_KEY: "not-a-table", "other": {"command": "x"}}}
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert plan.conflict_detail is not None
    assert plan.merged_config is None


# --- build_client_config_plan: update / preservation ----------------------


@pytest.mark.unit
def test_build_plan_claude_preserves_unrelated_keys_on_update(tmp_path: Path) -> None:
    """Verify a Claude update preserves unrelated servers and top-level keys."""
    config = {
        "mcpServers": {"other": {"type": "stdio", "command": "x", "args": []}},
        "numberOfStartups": 7,
    }
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    assert plan.backup_path == tmp_path / ".claude.json.awf-backup-20260603123045"
    assert plan.merged_config is not None
    assert plan.merged_config["mcpServers"]["other"]["command"] == "x"
    assert plan.merged_config["numberOfStartups"] == 7
    assert plan.merged_config["mcpServers"][AWF_MCP_SERVER_KEY]["args"] == _desired_args()


@pytest.mark.unit
def test_build_plan_codex_preserves_unrelated_tables_on_update(tmp_path: Path) -> None:
    """Verify a Codex update preserves unrelated tables in the merged config."""
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        '[mcp_servers.other]\ncommand = "other"\nargs = ["x"]\n\n[ui]\ntheme = "dark"\n',
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    assert plan.merged_config is not None
    assert plan.merged_config["mcp_servers"]["other"]["command"] == "other"
    assert plan.merged_config["ui"]["theme"] == "dark"


# --- build_client_config_plan: method selection ---------------------------


@pytest.mark.unit
def test_build_plan_selects_official_cli_when_present(tmp_path: Path) -> None:
    """Verify a resolvable client binary selects the official-CLI method."""
    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_found, now=_now
    )

    assert plan.method == "official_cli"
    assert plan.cli_command is not None
    assert plan.cli_command[0] == "claude"
    assert plan.cli_command[-1] == _ENV_FILE


@pytest.mark.unit
def test_build_plan_selects_file_when_cli_absent(tmp_path: Path) -> None:
    """Verify an absent client binary falls back to the file method."""
    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.method == "file"
    assert plan.cli_command is None


# --- apply_client_config_plan: file fallback ------------------------------


@pytest.mark.unit
def test_apply_file_create_writes_without_backup(tmp_path: Path) -> None:
    """Verify a create write produces the config with no backup file."""
    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    result = apply_client_config_plan(plan, run=_never_run)

    assert result.wrote is True
    assert result.backup_path is None
    written = json.loads(_claude_config_path(tmp_path).read_text(encoding="utf-8"))
    assert written["mcpServers"][AWF_MCP_SERVER_KEY]["args"] == _desired_args()
    assert not list(tmp_path.glob(".claude.json.awf-backup-*"))


@pytest.mark.unit
def test_apply_file_update_backs_up_and_preserves(tmp_path: Path) -> None:
    """Verify an update backs up the prior file and preserves unrelated config."""
    config = {
        "mcpServers": {"other": {"type": "stdio", "command": "x", "args": []}},
        "telemetry": True,
    }
    config_path = _claude_config_path(tmp_path)
    config_path.write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )
    result = apply_client_config_plan(plan, run=_never_run)

    assert result.action == "update"
    assert result.backup_path == config_path.with_name(".claude.json.awf-backup-20260603123045")
    assert result.backup_path is not None and result.backup_path.exists()
    backup = json.loads(result.backup_path.read_text(encoding="utf-8"))
    assert AWF_MCP_SERVER_KEY not in backup["mcpServers"]
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["mcpServers"]["other"]["command"] == "x"
    assert written["telemetry"] is True
    assert AWF_MCP_SERVER_KEY in written["mcpServers"]


@pytest.mark.unit
def test_apply_file_uses_plan_backup_path_not_recomputed(tmp_path: Path) -> None:
    """Verify apply writes the plan's stamped backup path, never re-stamping now().

    Regression: the apply path used to call now() a second time to derive the
    backup filename, so a second boundary between plan and apply would make the
    file on disk diverge from the dry-run preview (which reports plan.backup_path).
    Apply must consume plan.backup_path verbatim.
    """
    config_path = _claude_config_path(tmp_path)
    config_path.write_text(json.dumps({"telemetry": True}), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )
    assert plan.backup_path is not None

    result = apply_client_config_plan(plan, run=_never_run)

    assert result.backup_path == plan.backup_path
    assert plan.backup_path.exists()
    assert not [p for p in tmp_path.glob(".claude.json.awf-backup-*") if p != plan.backup_path]


@pytest.mark.unit
@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX permissions only")
def test_apply_file_write_leaves_owner_private_permissions(tmp_path: Path) -> None:
    """Verify the written config and its parent dir are owner-private."""
    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    apply_client_config_plan(plan, run=_never_run)

    codex_path = _codex_config_path(tmp_path)
    assert (codex_path.stat().st_mode & 0o777) == 0o600
    assert (codex_path.parent.stat().st_mode & 0o777) == 0o700
    reparsed = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    assert reparsed["mcp_servers"][AWF_MCP_SERVER_KEY]["args"] == _desired_args()


@pytest.mark.unit
def test_apply_conflict_raises_and_leaves_file_untouched(tmp_path: Path) -> None:
    """Verify applying a conflict plan raises and never mutates the file."""
    config = {"mcpServers": {AWF_MCP_SERVER_KEY: {"command": "other", "args": []}}}
    config_path = _claude_config_path(tmp_path)
    original = json.dumps(config)
    config_path.write_text(original, encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )
    with pytest.raises(SetupCheckError) as excinfo:
        apply_client_config_plan(plan, run=_never_run)

    assert excinfo.value.reason_code == CLIENT_CONFIG_CONFLICT
    assert config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".claude.json.awf-backup-*"))


@pytest.mark.unit
def test_apply_no_change_does_not_write(tmp_path: Path) -> None:
    """Verify a no_change plan reports wrote=False and writes nothing."""
    config = {
        "mcpServers": {
            AWF_MCP_SERVER_KEY: {"type": "stdio", "command": "awf", "args": _desired_args()}
        }
    }
    config_path = _claude_config_path(tmp_path)
    original = json.dumps(config)
    config_path.write_text(original, encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )
    result = apply_client_config_plan(plan, run=_never_run)

    assert result.wrote is False
    assert config_path.read_text(encoding="utf-8") == original


@pytest.mark.unit
def test_apply_file_write_failure_is_reason_coded(tmp_path: Path) -> None:
    """Verify an unwritable target yields CLIENT_CONFIG_WRITE_FAILED, no temp left."""
    # Make the config's parent path a regular file so mkdir/open fails with OSError.
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    config_path = blocker / "config.toml"

    descriptor = CLIENT_DESCRIPTORS["codex"]
    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )
    # Re-point the plan at the unwritable nested path.
    object.__setattr__(plan, "config_path", config_path)

    with pytest.raises(SetupCheckError) as excinfo:
        apply_client_config_plan(plan, run=_never_run)

    assert excinfo.value.reason_code == CLIENT_CONFIG_WRITE_FAILED
    assert not list(blocker.parent.glob("*.tmp"))
    _ = descriptor


# --- apply_client_config_plan: official CLI -------------------------------


@pytest.mark.unit
def test_apply_official_cli_invokes_expected_argv(tmp_path: Path) -> None:
    """Verify the official-CLI path runs the add command and writes no file."""
    runner = _FakeRunner(CommandResult(returncode=0))
    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_found, now=_now
    )

    result = apply_client_config_plan(plan, run=runner)

    assert result.wrote is True
    assert runner.calls == [
        (
            "claude",
            "mcp",
            "add",
            "--transport",
            "stdio",
            "--scope",
            "user",
            "awf",
            "--",
            "awf",
            "mcp",
            "serve",
            "--env-file",
            _ENV_FILE,
        )
    ]
    assert not _claude_config_path(tmp_path).exists()


@pytest.mark.unit
@pytest.mark.parametrize("result", [CommandResult(returncode=2, stderr="boom"), None])
def test_apply_official_cli_failure_is_reason_coded(
    tmp_path: Path, result: CommandResult | None
) -> None:
    """Verify a non-zero/None CLI result yields CLIENT_CONFIG_WRITE_FAILED."""
    runner = _FakeRunner(result)
    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_found, now=_now
    )

    with pytest.raises(SetupCheckError) as excinfo:
        apply_client_config_plan(plan, run=runner)

    assert excinfo.value.reason_code == CLIENT_CONFIG_WRITE_FAILED
    assert not _codex_config_path(tmp_path).exists()


# --- setup_client orchestration & security --------------------------------


@pytest.mark.unit
def test_setup_client_dry_run_does_not_mutate(tmp_path: Path) -> None:
    """Verify a dry-run produces a diff payload and mutates nothing."""
    config = {"mcpServers": {"other": {"type": "stdio", "command": "x", "args": []}}}
    config_path = _claude_config_path(tmp_path)
    original = json.dumps(config)
    config_path.write_text(original, encoding="utf-8")

    payload = setup_client(
        "claude",
        env_file=_ENV_FILE,
        dry_run=True,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    assert payload.status == "success"
    assert payload.details["action"] == "update"
    assert payload.details["dry_run"] is True
    assert _ENV_FILE in payload.details["diff"]
    assert config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".claude.json.awf-backup-*"))


@pytest.mark.unit
def test_setup_client_dry_run_conflict_is_blocked(tmp_path: Path) -> None:
    """Verify a dry-run conflict renders a blocked CLIENT_CONFIG_CONFLICT payload."""
    config = {"mcpServers": {AWF_MCP_SERVER_KEY: {"command": "other", "args": []}}}
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    payload = setup_client(
        "claude",
        env_file=_ENV_FILE,
        dry_run=True,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    assert payload.status == "blocked"
    assert payload.reason_code == CLIENT_CONFIG_CONFLICT


@pytest.mark.unit
def test_setup_client_apply_write_failure_folds_into_blocked_payload(tmp_path: Path) -> None:
    """Verify an apply write failure folds into a blocked payload, not a raise."""
    runner = _FakeRunner(CommandResult(returncode=1, stderr="nope"))

    payload = setup_client(
        "codex",
        env_file=_ENV_FILE,
        dry_run=False,
        home=tmp_path,
        which=_which_found,
        run=runner,
        now=_now,
    )

    assert payload.status == "blocked"
    assert payload.reason_code == CLIENT_CONFIG_WRITE_FAILED


@pytest.mark.unit
def test_setup_client_redacts_token_shaped_env_file_path(tmp_path: Path) -> None:
    """Verify a token-shaped env-file path is redacted in rendered output."""
    token_path = "/srv/ghp_abcdefghijklmnopqrstuvwxyz0123456789/.env"

    payload = setup_client(
        "claude",
        env_file=token_path,
        dry_run=True,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    rendered_json = json.dumps(render_first_run_json(payload))
    rendered_pretty = render_first_run_pretty(payload)
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in rendered_json
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in rendered_pretty
    assert REDACTION_MARKER in rendered_json


@pytest.mark.unit
def test_build_plan_never_reads_env_file_contents(tmp_path: Path) -> None:
    """Verify the helper never reads the env-file's contents into the plan/payload."""
    env_file = tmp_path / "compose.env"
    secret_contents = "AWF_GITHUB_TOKEN=ghp_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
    env_file.write_text(secret_contents, encoding="utf-8")

    payload = setup_client(
        "codex",
        env_file=env_file,
        dry_run=True,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    rendered = json.dumps(render_first_run_json(payload))
    assert "ghp_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz" not in rendered
    # The path itself is fine to surface; the file's body must never appear.
    assert "AWF_GITHUB_TOKEN=" not in rendered


# --- CODEX_HOME override --------------------------------------------------


@pytest.mark.unit
def test_build_plan_codex_honors_codex_home_override(tmp_path: Path) -> None:
    """Verify a set CODEX_HOME pins the Codex config under it, not home/.codex.

    Regression for PRRT_kwDOSJAM6s6GyZ1U: the official Codex CLI resolves
    ``$CODEX_HOME/config.toml`` (CODEX_HOME overrides ~/.codex), so the
    file-fallback write and the plan/conflict-check must target that same file
    instead of the hard-coded ``home/.codex/config.toml`` the client never loads.
    """
    codex_home = tmp_path / "xdg-codex"

    plan = build_client_config_plan(
        "codex",
        env_file=_ENV_FILE,
        home=tmp_path,
        which=_which_missing,
        now=_now,
        env={"CODEX_HOME": str(codex_home)},
    )

    assert plan.config_path == codex_home / "config.toml"
    assert plan.config_path != _codex_config_path(tmp_path)


@pytest.mark.unit
def test_apply_file_codex_writes_under_codex_home(tmp_path: Path) -> None:
    """Verify the file fallback writes the Codex config under CODEX_HOME."""
    codex_home = tmp_path / "xdg-codex"

    plan = build_client_config_plan(
        "codex",
        env_file=_ENV_FILE,
        home=tmp_path,
        which=_which_missing,
        now=_now,
        env={"CODEX_HOME": str(codex_home)},
    )
    apply_client_config_plan(plan, run=_never_run)

    written = (codex_home / "config.toml").read_text(encoding="utf-8")
    reparsed = tomllib.loads(written)
    assert reparsed["mcp_servers"][AWF_MCP_SERVER_KEY]["args"] == _desired_args()
    # The hard-coded home/.codex path is never created.
    assert not _codex_config_path(tmp_path).exists()


@pytest.mark.unit
def test_build_plan_codex_blank_codex_home_falls_back_to_home(tmp_path: Path) -> None:
    """Verify a blank/whitespace CODEX_HOME falls back to home/.codex/config.toml."""
    plan = build_client_config_plan(
        "codex",
        env_file=_ENV_FILE,
        home=tmp_path,
        which=_which_missing,
        now=_now,
        env={"CODEX_HOME": "   "},
    )

    assert plan.config_path == _codex_config_path(tmp_path)


@pytest.mark.unit
def test_build_plan_claude_ignores_codex_home(tmp_path: Path) -> None:
    """Verify CODEX_HOME never affects the Claude config path (no home override)."""
    plan = build_client_config_plan(
        "claude",
        env_file=_ENV_FILE,
        home=tmp_path,
        which=_which_missing,
        now=_now,
        env={"CODEX_HOME": str(tmp_path / "xdg-codex")},
    )

    assert plan.config_path == _claude_config_path(tmp_path)


# --- additional edge coverage ---------------------------------------------


@pytest.mark.unit
def test_build_plan_unreadable_existing_config_is_conflict(tmp_path: Path) -> None:
    """Verify an existing config path that cannot be read is an ambiguous conflict."""
    # A directory at the config path makes read_text raise IsADirectoryError (OSError).
    _claude_config_path(tmp_path).mkdir()

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert "could not be read" in (plan.conflict_detail or "")


@pytest.mark.unit
def test_codex_update_emits_bool_and_quoted_keys(tmp_path: Path) -> None:
    """Verify the scoped TOML emitter round-trips booleans and quoted keys."""
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        '[ui]\ndark_mode = true\n"weird.key" = "kept"\n',
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )
    apply_client_config_plan(plan, run=_never_run)

    reparsed = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    assert reparsed["ui"]["dark_mode"] is True
    assert reparsed["ui"]["weird.key"] == "kept"
    assert reparsed["mcp_servers"][AWF_MCP_SERVER_KEY]["args"] == _desired_args()


@pytest.mark.unit
def test_codex_update_quotes_non_ascii_keys(tmp_path: Path) -> None:
    """Verify non-ASCII alphanumeric keys are quoted so TOML stays valid.

    ``str.isalnum`` is True for non-ASCII letters (e.g. ``é``), but TOML bare
    keys are restricted to ASCII; emitting such a key unquoted yields invalid
    TOML, so the emitter must quote it and the result must round-trip.
    """
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    # Quoted non-ASCII keys parse cleanly, but their unquoted spelling is invalid
    # TOML — the emitter must re-quote them on the way out.
    codex_path.write_text('[ui]\n"café" = "kept"\n', encoding="utf-8")

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )
    apply_client_config_plan(plan, run=_never_run)

    rendered = codex_path.read_text(encoding="utf-8")
    # The bare (unquoted) spelling would be invalid TOML; it must be quoted.
    assert "\ncafé = " not in rendered
    reparsed = tomllib.loads(rendered)
    assert reparsed["ui"]["café"] == "kept"
    assert reparsed["mcp_servers"][AWF_MCP_SERVER_KEY]["args"] == _desired_args()


@pytest.mark.unit
def test_setup_client_apply_no_change_reports_success_without_diff(tmp_path: Path) -> None:
    """Verify a non-dry-run no_change apply succeeds with no diff/backup details."""
    config = {
        "mcpServers": {
            AWF_MCP_SERVER_KEY: {"type": "stdio", "command": "awf", "args": _desired_args()}
        }
    }
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    payload = setup_client(
        "claude",
        env_file=_ENV_FILE,
        dry_run=False,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    assert payload.status == "success"
    assert payload.details["action"] == "no_change"
    assert payload.details["wrote"] is False
    assert "diff" not in payload.details
    assert "backup_path" not in payload.details
    assert "no change needed" in payload.summary


@pytest.mark.unit
def test_setup_client_dry_run_no_change_summary(tmp_path: Path) -> None:
    """Verify the dry-run no_change summary path renders without a diff."""
    config = {
        "mcpServers": {
            AWF_MCP_SERVER_KEY: {"type": "stdio", "command": "awf", "args": _desired_args()}
        }
    }
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    payload = setup_client(
        "claude",
        env_file=_ENV_FILE,
        dry_run=True,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    assert payload.status == "success"
    assert payload.details["action"] == "no_change"
    assert "no change needed" in payload.summary


@pytest.mark.unit
def test_setup_client_cli_failure_without_stderr_is_reason_coded(tmp_path: Path) -> None:
    """Verify a CLI failure carrying no stderr still folds into a blocked payload."""
    runner = _FakeRunner(CommandResult(returncode=3))

    payload = setup_client(
        "claude",
        env_file=_ENV_FILE,
        dry_run=False,
        home=tmp_path,
        which=_which_found,
        run=runner,
        now=_now,
    )

    assert payload.status == "blocked"
    assert payload.reason_code == CLIENT_CONFIG_WRITE_FAILED
    issue_details = payload.issues[0].details
    assert issue_details["returncode"] == 3
    assert "stderr" not in issue_details


@pytest.mark.unit
def test_default_client_command_runner_missing_binary_returns_none() -> None:
    """Verify the default runner returns None when the binary cannot launch."""
    from awf.host_setup.clients import default_client_command_runner

    assert default_client_command_runner(["awf-nonexistent-binary-xyz", "--help"]) is None


@pytest.mark.unit
def test_default_client_command_runner_captures_result() -> None:
    """Verify the default runner captures a real command's return code/output."""
    import sys

    from awf.host_setup.clients import default_client_command_runner

    result = default_client_command_runner(
        [sys.executable, "-c", "import sys; print('hi'); sys.exit(0)"]
    )

    assert result is not None
    assert result.returncode == 0
    assert "hi" in result.stdout


# --- setup_clients: plan-all-then-apply (no partial writes) ---------------


@pytest.mark.unit
def test_setup_clients_applies_all_when_none_conflict(tmp_path: Path) -> None:
    """Verify a clean multi-client run writes every selected client's config."""
    payloads = setup_clients(
        ["claude", "codex"],
        env_file=_ENV_FILE,
        dry_run=False,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    assert [payload.status for payload in payloads] == ["success", "success"]
    assert [payload.details["wrote"] for payload in payloads] == [True, True]
    assert _claude_config_path(tmp_path).exists()
    assert _codex_config_path(tmp_path).exists()


@pytest.mark.unit
def test_setup_clients_applies_nothing_when_any_client_conflicts(tmp_path: Path) -> None:
    """Verify a conflict blocks the run and leaves the clean sibling unwritten.

    The clean claude client is listed first so, without plan-all-then-apply, it
    would be written before the conflicting codex client is even planned. The
    fix must leave no client config on disk.
    """
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    original = '[mcp_servers.awf]\ncommand = "other"\nargs = []\n'
    codex_path.write_text(original, encoding="utf-8")

    # Payloads are returned in the input order: claude (clean), codex (conflict).
    claude_payload, codex_payload = setup_clients(
        ["claude", "codex"],
        env_file=_ENV_FILE,
        dry_run=False,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    # The clean sibling is reported as planned-but-not-applied (never written);
    # the conflicting client renders a blocked conflict.
    assert claude_payload.status == "success"
    assert claude_payload.details.get("wrote") is not True
    assert "applied nothing" in claude_payload.summary
    assert codex_payload.status == "blocked"
    assert codex_payload.reason_code == CLIENT_CONFIG_CONFLICT
    # No client config was written: claude is absent and codex is untouched.
    assert not _claude_config_path(tmp_path).exists()
    assert codex_path.read_text(encoding="utf-8") == original


@pytest.mark.unit
def test_setup_clients_conflict_block_reports_no_change_sibling(tmp_path: Path) -> None:
    """Verify a no_change sibling is reported as such when another conflicts."""
    # claude already registers AWF (no_change); codex conflicts.
    _claude_config_path(tmp_path).write_text(
        json.dumps(
            {
                "mcpServers": {
                    AWF_MCP_SERVER_KEY: {
                        "type": "stdio",
                        "command": "awf",
                        "args": _desired_args(),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text('[mcp_servers.awf]\ncommand = "other"\nargs = []\n', encoding="utf-8")

    claude_payload, _codex_payload = setup_clients(
        ["claude", "codex"],
        env_file=_ENV_FILE,
        dry_run=False,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    assert claude_payload.status == "success"
    assert claude_payload.details["action"] == "no_change"
    assert "no change needed" in claude_payload.summary


@pytest.mark.unit
def test_setup_clients_dry_run_writes_nothing(tmp_path: Path) -> None:
    """Verify a dry-run multi-client plan renders payloads without mutating."""
    payloads = setup_clients(
        ["claude", "codex"],
        env_file=_ENV_FILE,
        dry_run=True,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    assert all(payload.details["dry_run"] is True for payload in payloads)
    assert not _claude_config_path(tmp_path).exists()
    assert not _codex_config_path(tmp_path).exists()


@pytest.mark.unit
def test_apply_conflict_without_detail_omits_conflict_detail(tmp_path: Path) -> None:
    """Verify a conflict plan lacking a detail still raises with no conflict_detail."""
    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )
    detailless = ClientConfigPlan(
        client="claude",
        method="file",
        config_path=plan.config_path,
        action="conflict",
    )

    with pytest.raises(SetupCheckError) as excinfo:
        apply_client_config_plan(detailless, run=_never_run)

    assert excinfo.value.reason_code == CLIENT_CONFIG_CONFLICT
    assert "conflict_detail" not in excinfo.value.details
