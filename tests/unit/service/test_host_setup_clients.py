"""Behavior, edge, and error coverage for client MCP plan building.

Covers ``normalize_client(s)`` and ``build_client_config_plan`` across its
create / no_change / conflict / update / method-selection branches. The
apply and orchestration helpers are exercised in the sibling part module.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from awf.host_setup.clients import (
    AWF_MCP_SERVER_KEY,
    KNOWN_SETUP_CLIENTS,
    _toml_text_has_comment,
    apply_client_config_plan,
    build_client_config_plan,
    normalize_client,
    normalize_clients,
)
from awf.host_setup.rendering import SETUP_CLIENT_UNKNOWN
from awf.host_setup.system_checks import SetupCheckError

from ._helpers import (
    _ENV_FILE,
    _claude_config_path,
    _codex_config_path,
    _desired_args,
    _never_run,
    _now,
    _which_found,
    _which_missing,
)

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
