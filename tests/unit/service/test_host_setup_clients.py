"""Behavior, edge, and error coverage for client MCP integration helpers."""

from __future__ import annotations

import json
import os
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
    _toml_text_has_comment,
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
@pytest.mark.parametrize("stale_type", ["http", "sse"])
def test_build_plan_claude_stale_transport_type_plans_update(
    tmp_path: Path, stale_type: str
) -> None:
    """Verify a Claude awf entry with a non-stdio transport ``type`` plans update.

    Claude MCP config uses ``type`` to distinguish stdio from HTTP/SSE
    transports. An ``awf`` entry whose command/args match but whose ``type`` is a
    stale/malformed non-stdio value would not launch ``awf mcp serve``; reporting
    it as ``no_change`` would claim AWF is registered while leaving a dead entry.
    The planner must instead route it to an ``update`` that rewrites the canonical
    ``"type": "stdio"`` transport.
    """
    config = {
        "mcpServers": {
            AWF_MCP_SERVER_KEY: {
                "type": stale_type,
                "command": "awf",
                "args": _desired_args(),
            }
        }
    }
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    assert plan.method == "file"
    assert plan.merged_config is not None
    assert plan.merged_config["mcpServers"][AWF_MCP_SERVER_KEY]["type"] == "stdio"


@pytest.mark.unit
@pytest.mark.parametrize("stale_type", ["http", "sse"])
def test_apply_claude_stale_type_update_writes_file_even_with_cli(
    tmp_path: Path, stale_type: str
) -> None:
    """Verify repairing an existing Claude awf entry uses the file write, not the CLI.

    Regression for PRRT_kwDOSJAM6s6G30y2: when the ``claude`` CLI is on PATH the
    method defaulted to ``official_cli``, so applying an ``update`` that only fixes
    a drifted required field (the stale non-stdio transport ``type``) shelled out
    to ``claude mcp add`` — an *add*, which cannot rewrite an existing 'awf' entry —
    and ignored the corrected ``merged_config``. The fix forces the structured file
    write for an existing-entry update, so the broken ``type`` is actually repaired
    and the CLI is never invoked.
    """
    config = {
        "mcpServers": {
            AWF_MCP_SERVER_KEY: {
                "type": stale_type,
                "command": "awf",
                "args": _desired_args(),
            }
        }
    }
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_found, now=_now
    )

    assert plan.action == "update"
    assert plan.method == "file"
    assert plan.cli_command is None

    # ``_never_run`` asserts the official CLI is not invoked; the file write must
    # repair the on-disk transport ``type`` instead of leaving it broken.
    result = apply_client_config_plan(plan, run=_never_run)

    assert result.wrote is True
    written = json.loads(_claude_config_path(tmp_path).read_text(encoding="utf-8"))
    assert written["mcpServers"][AWF_MCP_SERVER_KEY]["type"] == "stdio"


@pytest.mark.unit
def test_build_plan_codex_full_matching_entry_is_no_change(tmp_path: Path) -> None:
    """Verify a Codex entry matching command/args and bounded timeouts is no_change."""
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        "[mcp_servers.awf]\n"
        'command = "awf"\n'
        f'args = ["mcp", "serve", "--env-file", "{_ENV_FILE}"]\n'
        "startup_timeout_sec = 20\n"
        "tool_timeout_sec = 120\n",
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "no_change"
    assert plan.diff == ""


@pytest.mark.unit
def test_build_plan_codex_missing_timeouts_plans_update(tmp_path: Path) -> None:
    """Verify a Codex entry matching command/args but lacking AWF's bounded
    startup/tool timeouts plans an update that writes them, rather than a no_change.

    An ``awf`` entry created by ``codex mcp add`` (or by an AWF setup predating
    the direct file-write path) has the right command/args but no timeouts; the
    file write must add the bounded timeouts AWF requires.
    """
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        "[mcp_servers.awf]\n"
        'command = "awf"\n'
        f'args = ["mcp", "serve", "--env-file", "{_ENV_FILE}"]\n',
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    assert plan.method == "file"
    assert "startup_timeout_sec = 20" in plan.diff
    assert "tool_timeout_sec = 120" in plan.diff
    assert plan.merged_config is not None
    assert plan.merged_config["mcp_servers"][AWF_MCP_SERVER_KEY]["startup_timeout_sec"] == 20
    assert plan.merged_config["mcp_servers"][AWF_MCP_SERVER_KEY]["tool_timeout_sec"] == 120


@pytest.mark.unit
def test_build_plan_codex_stale_timeout_plans_update(tmp_path: Path) -> None:
    """Verify a Codex entry with a stale bounded timeout is refreshed via update."""
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        "[mcp_servers.awf]\n"
        'command = "awf"\n'
        f'args = ["mcp", "serve", "--env-file", "{_ENV_FILE}"]\n'
        "startup_timeout_sec = 99\n"
        "tool_timeout_sec = 120\n",
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    assert plan.merged_config is not None
    assert plan.merged_config["mcp_servers"][AWF_MCP_SERVER_KEY]["startup_timeout_sec"] == 20


@pytest.mark.unit
def test_build_plan_codex_update_dropping_extra_fields_flags_approximate(
    tmp_path: Path,
) -> None:
    """Verify an update that drops a user-added ``awf`` field flags the diff approximate.

    A Codex ``awf`` entry with matching command/args but missing AWF's bounded
    timeouts plus a manually configured ``env`` subtable routes to ``update``;
    ``_merged_config`` replaces the whole entry wholesale, deleting the ``env``
    subtable. Those removed lines all echo pre-existing content, so the scoped
    diff filter suppresses them — the preview must then be flagged approximate so
    the destructive drop is not hidden behind a "just added timeouts" preview.
    """
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        "[mcp_servers.awf]\n"
        'command = "awf"\n'
        f'args = ["mcp", "serve", "--env-file", "{_ENV_FILE}"]\n'
        "\n"
        "[mcp_servers.awf.env]\n"
        'MY_VAR = "kept-by-user"\n',
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    assert plan.method == "file"
    # The write genuinely drops the user's env subtable (AWF owns its entry).
    assert plan.merged_config is not None
    assert "env" not in plan.merged_config["mcp_servers"][AWF_MCP_SERVER_KEY]
    # The removed env lines echo existing content and are suppressed from the
    # preview, so the diff must be flagged approximate rather than hiding the loss.
    assert plan.diff_is_approximate is True
    assert "MY_VAR" not in plan.diff


@pytest.mark.unit
def test_build_plan_diff_excludes_unrelated_existing_secrets(tmp_path: Path) -> None:
    """Verify the update diff is scoped to the awf entry and never quotes a
    neighboring server's secret as surrounding context.

    Existing client configs commonly hold other MCP servers whose env entries
    carry API keys. AWF only ever adds/changes its own ``awf`` entry, so the
    diff must not surface adjacent secret values as diff context where the
    payload redactor (which only catches known token shapes) could miss them.
    """
    secret = "raw-unmatched-deployment-secret-value"
    config = {
        "mcpServers": {
            "aaa-other": {
                "type": "stdio",
                "command": "other",
                "args": [],
                "env": {"DEPLOY_TOKEN": secret},
            }
        }
    }
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    assert AWF_MCP_SERVER_KEY in plan.diff
    assert _ENV_FILE in plan.diff
    assert secret not in plan.diff
    # The unrelated server's env key must not appear as diff context either.
    assert "DEPLOY_TOKEN" not in plan.diff


def test_build_plan_diff_excludes_adjacent_secret_gaining_trailing_comma(
    tmp_path: Path,
) -> None:
    """Verify a top-level sibling secret is not leaked via JSON comma rewrite.

    Appending AWF's ``mcpServers`` key makes the previous last JSON key gain a
    structural trailing comma, so an ``n=0`` diff would otherwise emit that
    pre-existing line (e.g. ``"apiKey": "<secret>"``) as both a removal and a
    comma-suffixed addition — bypassing the token redactor for camelCase/other
    unrecognized secret shapes. The diff must scope itself to AWF's brand-new
    lines only.
    """
    secret = "unrecognized-camelCase-apikey-value"
    config = {"apiKey": secret}
    _claude_config_path(tmp_path).write_text(json.dumps(config, indent=2), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    assert AWF_MCP_SERVER_KEY in plan.diff
    assert _ENV_FILE in plan.diff
    assert secret not in plan.diff
    # The pre-existing key itself must not be echoed as a comma-churn line.
    assert "apiKey" not in plan.diff
    # The only suppression here is the benign comma rewrite of the prior last key
    # (offset by its matching removal), so the preview is still complete.
    assert plan.diff_is_approximate is False


@pytest.mark.unit
def test_build_plan_diff_flags_approximate_when_added_line_matches_existing(
    tmp_path: Path,
) -> None:
    """Verify a genuine AWF line dropped by the secret filter flags the diff approximate.

    When another ``stdio`` server already exists, AWF's brand-new ``"type": "stdio"``
    line is identical to a pre-existing line, so the secret-leak suppression filter
    drops it from the preview even though the write adds it. The diff must then be
    marked approximate (like the official-CLI path) so the operator is not misled
    into thinking the (now incomplete) preview is the full set of added lines.
    """
    config = {
        "mcpServers": {
            "aaa-other": {"type": "stdio", "command": "other", "args": []},
        }
    }
    _claude_config_path(tmp_path).write_text(json.dumps(config, indent=2), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    assert plan.method == "file"
    # The added ``"type": "stdio"`` line is suppressed (it echoes the sibling
    # server's identical line), so the diff is incomplete and flagged approximate.
    assert plan.diff_is_approximate is True
    assert '"type": "stdio"' not in plan.diff
    # The write itself stays exact: the merged config still carries AWF's entry.
    assert plan.merged_config["mcpServers"][AWF_MCP_SERVER_KEY]["type"] == "stdio"


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
    # Same binary ('awf'), only the --env-file path moved: the detail must surface
    # the concrete same-command/different-args case, not just "command/args".
    assert "--env-file" in (plan.conflict_detail or "")
    assert "command is unchanged" in (plan.conflict_detail or "")


@pytest.mark.unit
def test_build_plan_claude_different_command_conflict_omits_env_file_hint(
    tmp_path: Path,
) -> None:
    """Verify a genuinely different binary keeps the generic detail, no path hint.

    The env-file hint is reserved for the same-command/different-args case; a
    wholly different ``command`` must not claim the difference is merely a moved
    ``--env-file`` path.
    """
    config = {
        "mcpServers": {
            AWF_MCP_SERVER_KEY: {"type": "stdio", "command": "other-binary", "args": ["--serve"]}
        }
    }
    _claude_config_path(tmp_path).write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert "different command/args" in (plan.conflict_detail or "")
    assert "--env-file" not in (plan.conflict_detail or "")


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
def test_build_plan_invalid_utf8_config_is_conflict(tmp_path: Path) -> None:
    """Verify an existing config with invalid UTF-8 bytes is an ambiguous conflict.

    ``Path.read_text(encoding="utf-8")`` raises ``UnicodeDecodeError`` (a
    ``ValueError`` subclass, not ``OSError``) on malformed bytes. Without a
    dedicated guard it would escape ``awf setup`` as an uncaught traceback
    instead of the reason-coded client config conflict.
    """
    _claude_config_path(tmp_path).write_bytes(b"\xff\xfe invalid utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert "JSON" in (plan.conflict_detail or "")
    assert plan.merged_config is None


@pytest.mark.unit
def test_build_plan_duplicate_json_keys_is_conflict(tmp_path: Path) -> None:
    """Verify duplicate top-level JSON keys are refused, not silently reduced.

    ``json.loads`` keeps only the last value for a repeated key, so a
    hand-edited ``~/.claude.json`` with two ``mcpServers`` blocks would be
    rewritten with the earlier block dropped. AWF must surface this as an
    ambiguous conflict instead of silently deleting the duplicate.
    """
    _claude_config_path(tmp_path).write_text(
        '{"mcpServers": {"a": {"command": "x"}}, "mcpServers": {"b": {"command": "y"}}}',
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert "duplicate" in (plan.conflict_detail or "")
    assert "mcpServers" in (plan.conflict_detail or "")
    assert plan.merged_config is None


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
def test_build_plan_codex_commented_config_update_is_conflict(tmp_path: Path) -> None:
    """Verify an update of a commented Codex config is refused, not silently rewritten.

    ``tomllib`` drops comments on parse, so rewriting the file would delete the
    user's documentation while the comment-free scoped diff shows only the AWF
    additions. AWF refuses such an update rather than destroy unrelated content.
    """
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        "# my hand-tuned codex config\n"
        "[mcp_servers.awf]\n"
        'command = "awf"\n'
        f'args = ["mcp", "serve", "--env-file", "{_ENV_FILE}"]\n',
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "conflict"
    assert "comments" in (plan.conflict_detail or "")
    assert plan.merged_config is None


@pytest.mark.unit
def test_build_plan_codex_commented_config_no_change_is_not_refused(tmp_path: Path) -> None:
    """Verify a comment never wedges a Codex config that already matches AWF.

    A fully matching ``awf`` entry plans a ``no_change`` that never writes the
    file, so the comments are preserved and the comment refusal must not fire.
    """
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        "# keep this comment\n"
        "[mcp_servers.awf]\n"
        'command = "awf"\n'
        f'args = ["mcp", "serve", "--env-file", "{_ENV_FILE}"]\n'
        "startup_timeout_sec = 20\n"
        "tool_timeout_sec = 120\n",
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "no_change"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# a comment\n", True),
        ("key = 1 # trailing comment\n", True),
        ("key = 1\n", False),
        ('key = "value with # inside"\n', False),
        ("key = 'literal # not a comment'\n", False),
        ('key = "escaped quote \\" then # still string"\n', False),
        ('key = """\nmultiline # not a comment\n"""\n', False),
        ("key = '''\nliteral multiline # not a comment\n'''\n", False),
        ('key = """unterminated # treated as string\n', False),
        ('comment_after = "s"  # real comment\n', True),
    ],
)
def test_toml_text_has_comment_detects_only_real_comments(text: str, expected: bool) -> None:
    """Verify the scanner flags ``#`` outside strings and ignores in-string hashes."""
    assert _toml_text_has_comment(text) is expected


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


@pytest.mark.unit
def test_build_plan_codex_update_diff_omits_bare_container_header(tmp_path: Path) -> None:
    """Verify the dry-run diff has no spurious bare ``[mcp_servers]`` header.

    Regression for issue:4613075619 (#2): when an existing Codex config holds only
    ``[mcp_servers.<name>]`` sub-tables and no scalar keys directly under
    ``mcp_servers``, the scoped TOML emitter must not re-emit a bare
    ``[mcp_servers]`` section header. Doing so surfaced a spurious ``+[mcp_servers]``
    line in the preview that read like a deliberate semantic change when it is only
    a round-trip serialization artifact. Only the actual AWF addition belongs in
    the added lines.
    """
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        '[mcp_servers.other]\ncommand = "other"\nargs = ["x"]\n',
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    assert "[mcp_servers.awf]" in plan.diff
    # The bare container header is redundant when only sub-tables exist; it must
    # not appear as an added line.
    assert "+[mcp_servers]" not in plan.diff
    # Applying still round-trips both servers despite the omitted container header.
    apply_client_config_plan(plan, run=_never_run)
    reparsed = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    assert reparsed["mcp_servers"]["other"]["command"] == "other"
    assert reparsed["mcp_servers"][AWF_MCP_SERVER_KEY]["args"] == _desired_args()


@pytest.mark.unit
def test_build_plan_codex_update_preserves_empty_leaf_table(tmp_path: Path) -> None:
    """Verify an empty leaf table keeps its header when re-emitted.

    Skipping bare container headers (issue:4613075619 #2) must not also drop a
    deliberately empty leaf table such as ``[tools]`` — its header is its only
    representation, so the emitter still emits a header for a scalar-less, sub-table-
    less node.
    """
    codex_path = _codex_config_path(tmp_path)
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        '[mcp_servers.other]\ncommand = "other"\nargs = ["x"]\n\n[tools]\n',
        encoding="utf-8",
    )

    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    assert plan.action == "update"
    apply_client_config_plan(plan, run=_never_run)
    written = codex_path.read_text(encoding="utf-8")
    assert "[tools]" in written
    reparsed = tomllib.loads(written)
    assert reparsed["tools"] == {}


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


@pytest.mark.unit
def test_build_plan_codex_uses_file_even_with_cli_present(tmp_path: Path) -> None:
    """Verify Codex takes the file path even when its CLI is on PATH.

    Regression for PRRT_kwDOSJAM6s6GyZ1X: ``codex mcp add`` cannot persist the
    bounded ``startup_timeout_sec``/``tool_timeout_sec`` AWF registers, so the
    official-CLI apply would silently install Codex defaults and diverge from the
    promised plan. Codex must therefore use the structured file write — which
    embeds the timeouts — even when ``codex`` resolves on PATH.
    """
    plan = build_client_config_plan(
        "codex", env_file=_ENV_FILE, home=tmp_path, which=_which_found, now=_now
    )

    assert plan.method == "file"
    assert plan.cli_command is None
    # The file write registers the bounded timeouts the CLI path would have dropped.
    assert plan.desired_entry["startup_timeout_sec"] == 20
    assert plan.desired_entry["tool_timeout_sec"] == 120
    assert "startup_timeout_sec = 20" in plan.diff
    assert "tool_timeout_sec = 120" in plan.diff


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
def test_apply_file_update_preserves_existing_key_order(tmp_path: Path) -> None:
    """Verify an update appends the AWF entry without reordering the whole file.

    Regression: serializing with ``sort_keys=True`` alphabetised every key in
    the user's config on each write, mutating far more of the file than the
    ``awf`` entry AWF actually owns. The write must preserve the existing
    on-disk key order and only add/refresh the ``awf`` server entry.
    """
    # Keys deliberately out of alphabetical order; "mcpServers" sorts before
    # both, so a sorting serializer would hoist it to the front.
    config = {"projects": {"/tmp/x": {}}, "userID": "abc"}
    config_path = _claude_config_path(tmp_path)
    config_path.write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )
    apply_client_config_plan(plan, run=_never_run)

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert list(written.keys()) == ["projects", "userID", "mcpServers"]
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
def test_apply_file_update_preserves_symlinked_config(tmp_path: Path) -> None:
    """Verify an update through a symlinked config writes the real target in place.

    Regression: the atomic replace renamed the temp file over the config path
    itself, so a dotfiles-managed symlink (e.g. ``~/.claude.json`` -> a tracked
    file) was destroyed and the real managed file left stale. The write must
    resolve the link and update the target, keeping the symlink intact.
    """
    real_target = tmp_path / "dotfiles" / "claude.json"
    real_target.parent.mkdir(parents=True)
    real_target.write_text(json.dumps({"telemetry": True}), encoding="utf-8")
    config_path = _claude_config_path(tmp_path)
    config_path.symlink_to(real_target)

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )
    apply_client_config_plan(plan, run=_never_run)

    # The link survives and still points at the managed file.
    assert config_path.is_symlink()
    assert config_path.resolve() == real_target.resolve()
    # The merged config landed in the real target, preserving unrelated keys.
    written = json.loads(real_target.read_text(encoding="utf-8"))
    assert written["telemetry"] is True
    assert AWF_MCP_SERVER_KEY in written["mcpServers"]


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
@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX permissions only")
def test_apply_file_leaves_existing_parent_permissions_untouched(tmp_path: Path) -> None:
    """Verify the file fallback never chmods an already-existing parent dir.

    Regression: Claude's file fallback writes ``~/.claude.json`` whose parent is
    ``$HOME``. Tightening a pre-existing parent to 0o700 would clobber
    intentionally shared home-directory permissions, so only a parent AWF
    actually created (e.g. ``~/.codex``) may be tightened.
    """
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o755)
    assert (home.stat().st_mode & 0o777) == 0o755

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=home, which=_which_missing, now=_now
    )

    apply_client_config_plan(plan, run=_never_run)

    config_path = _claude_config_path(home)
    assert config_path.exists()
    assert (config_path.stat().st_mode & 0o777) == 0o600
    assert (home.stat().st_mode & 0o777) == 0o755


@pytest.mark.unit
@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX permissions only")
def test_apply_file_backup_is_owner_private(tmp_path: Path) -> None:
    """Verify the backup of a prior config is created owner-private (0o600).

    Regression: the backup was written with ``Path.write_text`` (umask perms,
    typically 0o644) and only narrowed afterwards by a best-effort chmod that
    swallows ``OSError``. A failed chmod left a verbatim copy of a config that
    may hold API keys/tokens world-readable. The backup must be created 0o600
    from the start (os.open), independent of any later chmod. A permissive umask
    is forced here so the old write_text path would have produced 0o666.
    """
    config = {"mcpServers": {}, "telemetry": True}
    config_path = _claude_config_path(tmp_path)
    config_path.write_text(json.dumps(config), encoding="utf-8")

    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_missing, now=_now
    )

    old_umask = os.umask(0o000)
    try:
        result = apply_client_config_plan(plan, run=_never_run)
    finally:
        os.umask(old_umask)

    assert result.backup_path is not None and result.backup_path.exists()
    assert (result.backup_path.stat().st_mode & 0o777) == 0o600


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
    # Claude is the only client that uses the official-CLI apply path; Codex's CLI
    # cannot apply AWF's timeouts, so it always takes the structured file write.
    plan = build_client_config_plan(
        "claude", env_file=_ENV_FILE, home=tmp_path, which=_which_found, now=_now
    )

    with pytest.raises(SetupCheckError) as excinfo:
        apply_client_config_plan(plan, run=runner)

    assert excinfo.value.reason_code == CLIENT_CONFIG_WRITE_FAILED
    assert not _claude_config_path(tmp_path).exists()


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

    # Use Claude so the official-CLI apply (which the failing runner models) runs;
    # Codex always takes the file path because its CLI cannot apply AWF's timeouts.
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
def test_build_plan_codex_expands_tilde_in_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a ``~``-prefixed CODEX_HOME is expanded, not taken literally.

    Regression for PRRT_kwDOSJAM6s6G1gpw: a ``CODEX_HOME=~/.codex`` set outside
    a shell (no shell expansion) must resolve under the real home directory the
    Codex CLI loads, not a literal ``~`` directory relative to the cwd.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    plan = build_client_config_plan(
        "codex",
        env_file=_ENV_FILE,
        home=tmp_path,
        which=_which_missing,
        now=_now,
        env={"CODEX_HOME": "~/.codex"},
    )

    assert plan.config_path == tmp_path / ".codex" / "config.toml"


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
    # Regression for issue:4613075619 (#1): a no_change dry-run has nothing to
    # apply, so its next_steps must not tell the operator to re-run without
    # --dry-run as create/update plans do.
    assert payload.next_steps == (
        "The Claude Code MCP client already registers the AWF server; no action needed.",
    )
    assert not any("--dry-run" in step for step in payload.next_steps)


@pytest.mark.unit
def test_setup_client_dry_run_official_cli_marks_diff_approximate(tmp_path: Path) -> None:
    """Verify an official-CLI dry-run flags its file-derived diff as approximate.

    The apply shells out to ``claude mcp add`` rather than writing the previewed
    file, so the diff cannot be guaranteed byte-for-byte. The payload must say so
    and surface the exact argv that will run.
    """
    payload = setup_client(
        "claude",
        env_file=_ENV_FILE,
        dry_run=True,
        home=tmp_path,
        which=_which_found,
        run=_never_run,
        now=_now,
    )

    assert payload.status == "success"
    assert payload.details["method"] == "official_cli"
    assert payload.details["action"] == "create"
    assert payload.details["diff_is_approximate"] is True
    assert payload.details["cli_command"][0] == "claude"
    assert payload.details["cli_command"][-1] == _ENV_FILE


@pytest.mark.unit
def test_setup_client_dry_run_file_method_omits_approximate_marker(tmp_path: Path) -> None:
    """Verify the file-method dry-run carries no approximate-diff marker.

    The file path writes exactly the previewed merge, so the diff is authoritative
    and must not be labelled approximate or carry a CLI argv.
    """
    payload = setup_client(
        "claude",
        env_file=_ENV_FILE,
        dry_run=True,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    assert payload.details["method"] == "file"
    assert "diff_is_approximate" not in payload.details
    assert "cli_command" not in payload.details


@pytest.mark.unit
def test_setup_client_dry_run_file_method_marks_approximate_when_line_suppressed(
    tmp_path: Path,
) -> None:
    """Verify the file-method dry-run flags an incomplete diff as approximate.

    A sibling ``stdio`` server makes AWF's new ``"type": "stdio"`` line identical
    to a pre-existing one, so the secret-leak filter drops it from the preview.
    The payload must mark the diff approximate (without a CLI argv, which is an
    official-CLI-only field) so the operator knows the preview is incomplete.
    """
    config = {"mcpServers": {"aaa-other": {"type": "stdio", "command": "x", "args": []}}}
    _claude_config_path(tmp_path).write_text(json.dumps(config, indent=2), encoding="utf-8")

    payload = setup_client(
        "claude",
        env_file=_ENV_FILE,
        dry_run=True,
        home=tmp_path,
        which=_which_missing,
        run=_never_run,
        now=_now,
    )

    assert payload.details["method"] == "file"
    assert payload.details["diff_is_approximate"] is True
    assert "cli_command" not in payload.details


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
    # A no_change sibling has nothing to re-run, so its guidance must not tell the
    # operator to re-run setup for "these clients" like a planned create/update does.
    assert claude_payload.next_steps == (
        "This client is already correctly configured; only re-run setup for the "
        "conflicting client reported above.",
    )


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
