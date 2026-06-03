"""Client-integration helpers for registering AWF's stdio MCP server.

This module wires AWF's local stdio MCP server into the **Claude Code**
(``~/.claude.json``, JSON) and **Codex** (``~/.codex/config.toml``, TOML) client
config. It mirrors the secret-safe shape of ``credentials.py``: the only value
threaded through is the **env-file path** the registered server should read at
runtime -- a path string, never the file's contents. The helpers never read,
accept, store, or return provider tokens.

The flow is split into pure read+compute (:func:`build_client_config_plan`) and
mutation (:func:`apply_client_config_plan`), so a dry-run produces a unified diff
and planned action without touching the filesystem or invoking any external CLI.
When the official client CLI is present it is preferred for the apply; otherwise
the helpers fall back to structured JSON/TOML parsing, write a timestamped backup
before replacing an existing file, and refuse ambiguous conflicts. The official
CLI, the ``which`` detector, the home directory, and the clock are all
dependency-injected so tests are fully hermetic.
"""

from __future__ import annotations

import json
import os
import secrets
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import unified_diff
from pathlib import Path
from typing import Any, Literal

from awf.host_setup.rendering import (
    CLIENT_CONFIG_CONFLICT,
    CLIENT_CONFIG_WRITE_FAILED,
    SETUP_CLIENT_UNKNOWN,
    FirstRunPayload,
    first_run_failure_payload,
    first_run_success_payload,
)
from awf.host_setup.system_checks import (
    SETUP_COMMAND,
    CommandResult,
    CommandRunner,
    SetupCheckError,
    WhichFn,
)

NowFn = Callable[[], datetime]

# Canonical client selectors and the aliases operators commonly type, kept
# consistent with the provider aliasing in ``system_checks``.
KNOWN_SETUP_CLIENTS: tuple[str, ...] = ("claude", "codex")
_CLIENT_ALIASES: Mapping[str, str] = {
    "claude_code": "claude",
    "claudecode": "claude",
    "anthropic": "claude",
    "openai": "codex",
}

# The server-entry key both client configs register AWF's MCP server under.
AWF_MCP_SERVER_KEY = "awf"

# The argv AWF tells the registered server to run; the env-file path is appended
# per call. Shared by the desired-entry builder and the official-CLI add command.
_MCP_SERVE_ARGS: tuple[str, ...] = ("mcp", "serve", "--env-file")
_AWF_BINARY = "awf"

# Codex's MCP server table carries bounded startup/tool timeouts (see
# docs/MCP_SETUP.md); these mirror the documented defaults.
_CODEX_STARTUP_TIMEOUT_SEC = 20
_CODEX_TOOL_TIMEOUT_SEC = 120

_PROBE_TIMEOUT_SECONDS = 30.0

ClientConfigAction = Literal["create", "update", "no_change", "conflict"]
ClientConfigMethod = Literal["official_cli", "file"]
ClientConfigFormat = Literal["json", "toml"]


class _TomlEmitError(ValueError):
    """Raised when the scoped TOML emitter cannot represent a value."""


@dataclass(frozen=True)
class ClientDescriptor:
    """Static description of one supported MCP client integration."""

    key: str
    label: str
    config_format: ClientConfigFormat
    cli_binary: str
    # The relative config path under the resolved home directory.
    config_relative_path: tuple[str, ...]
    # The top-level mapping key that holds named MCP server entries.
    servers_key: str

    def config_path(self, home: Path) -> Path:
        """Return the absolute client config path under ``home``."""
        return home.joinpath(*self.config_relative_path)

    def desired_entry(self, env_file: str) -> dict[str, Any]:
        """Return the desired ``awf`` server entry pointing at ``env_file``."""
        args = [*_MCP_SERVE_ARGS, env_file]
        if self.config_format == "toml":
            return {
                "command": _AWF_BINARY,
                "args": args,
                "startup_timeout_sec": _CODEX_STARTUP_TIMEOUT_SEC,
                "tool_timeout_sec": _CODEX_TOOL_TIMEOUT_SEC,
            }
        return {"type": "stdio", "command": _AWF_BINARY, "args": args}

    def add_command(self, env_file: str) -> tuple[str, ...]:
        """Return the official-CLI argv that registers the AWF MCP server."""
        if self.key == "claude":
            # ``user`` scope writes the all-projects ``mcpServers`` block in
            # ``~/.claude.json`` — the same home config the file fallback edits.
            # ``local`` would instead register a cwd-only entry, diverging from
            # the plan/diff and the fallback path.
            return (
                self.cli_binary,
                "mcp",
                "add",
                "--transport",
                "stdio",
                "--scope",
                "user",
                AWF_MCP_SERVER_KEY,
                "--",
                _AWF_BINARY,
                *_MCP_SERVE_ARGS,
                env_file,
            )
        # Codex registers MCP servers via ``codex mcp add <name> -- <cmd> <args>``.
        return (
            self.cli_binary,
            "mcp",
            "add",
            AWF_MCP_SERVER_KEY,
            "--",
            _AWF_BINARY,
            *_MCP_SERVE_ARGS,
            env_file,
        )


CLIENT_DESCRIPTORS: Mapping[str, ClientDescriptor] = {
    "claude": ClientDescriptor(
        key="claude",
        label="Claude Code",
        config_format="json",
        cli_binary="claude",
        config_relative_path=(".claude.json",),
        servers_key="mcpServers",
    ),
    "codex": ClientDescriptor(
        key="codex",
        label="Codex",
        config_format="toml",
        cli_binary="codex",
        config_relative_path=(".codex", "config.toml"),
        servers_key="mcp_servers",
    ),
}


@dataclass(frozen=True)
class ClientConfigPlan:
    """A read-only, secret-free plan for one client MCP integration.

    Carries no token-shaped data: ``desired_entry`` and the diff only embed the
    env-file *path* the registered server should read, never its contents.
    """

    client: str
    method: ClientConfigMethod
    config_path: Path
    action: ClientConfigAction
    diff: str = ""
    backup_path: Path | None = None
    conflict_detail: str | None = None
    desired_entry: Mapping[str, Any] = field(default_factory=dict)
    existing_entry: Mapping[str, Any] | None = None
    # The full merged top-level config the file-fallback apply would write; set
    # only for create/update plans (None for no_change/conflict and official_cli).
    merged_config: Mapping[str, Any] | None = None
    # The official-CLI argv to invoke; set only when ``method == "official_cli"``.
    cli_command: tuple[str, ...] | None = None
    descriptor: ClientDescriptor | None = None


@dataclass(frozen=True)
class ClientWriteResult:
    """Outcome of applying a client MCP integration plan."""

    client: str
    method: ClientConfigMethod
    config_path: Path
    action: ClientConfigAction
    wrote: bool
    backup_path: Path | None = None


def normalize_client(name: str) -> str:
    """Normalize a client selector to a known canonical name.

    Raises ``SetupCheckError(SETUP_CLIENT_UNKNOWN)`` for unsupported names so
    setup never silently configures an unintended client.
    """
    normalized = name.strip().lower().replace("-", "_")
    normalized = _CLIENT_ALIASES.get(normalized, normalized)
    if normalized not in CLIENT_DESCRIPTORS:
        raise SetupCheckError(
            f"Unsupported client selector: {name!r}.",
            reason_code=SETUP_CLIENT_UNKNOWN,
            details={"client": name, "known_clients": list(KNOWN_SETUP_CLIENTS)},
        )
    return normalized


def normalize_clients(names: Iterable[str]) -> list[str]:
    """Normalize and de-duplicate client selectors while preserving order."""
    ordered: list[str] = []
    for name in names:
        normalized = normalize_client(name)
        if normalized not in ordered:
            ordered.append(normalized)
    return ordered


def build_client_config_plan(
    client: str,
    *,
    env_file: str | Path,
    home: Path,
    which: WhichFn,
    now: NowFn = lambda: datetime.now(UTC),
) -> ClientConfigPlan:
    """Compute a read-only plan for registering AWF's MCP server into ``client``.

    Reads only the single known client config path (no home-dir scanning) via a
    structured parse; an unparseable or non-mapping existing config is an
    ambiguous ``conflict`` that refuses to write. The env-file path is used only
    as a string; the file's contents are never read.
    """
    descriptor = CLIENT_DESCRIPTORS[client]
    config_path = descriptor.config_path(home)
    env_file_str = str(env_file)
    desired_entry = descriptor.desired_entry(env_file_str)
    method: ClientConfigMethod = "official_cli" if which(descriptor.cli_binary) else "file"

    existing, parse_error = _read_existing_config(config_path, descriptor.config_format)
    if parse_error is not None:
        return ClientConfigPlan(
            client=client,
            method=method,
            config_path=config_path,
            action="conflict",
            conflict_detail=parse_error,
            desired_entry=desired_entry,
            cli_command=descriptor.add_command(env_file_str) if method == "official_cli" else None,
            descriptor=descriptor,
        )

    existing_servers = existing.get(descriptor.servers_key) if existing is not None else None
    existing_entry: Mapping[str, Any] | None = None
    if (
        existing is not None
        and descriptor.servers_key in existing
        and not isinstance(existing_servers, Mapping)
    ):
        # A present-but-non-table servers block is ambiguous: merging would erase
        # it. Refuse the conflict rather than silently overwrite the value.
        return ClientConfigPlan(
            client=client,
            method=method,
            config_path=config_path,
            action="conflict",
            conflict_detail=(
                f"Existing {_label(client)} config has a non-table "
                f"'{descriptor.servers_key}' value; AWF refuses to overwrite it. "
                "Resolve it manually, then re-run."
            ),
            desired_entry=desired_entry,
            cli_command=descriptor.add_command(env_file_str) if method == "official_cli" else None,
            descriptor=descriptor,
        )
    if isinstance(existing_servers, Mapping):
        candidate = existing_servers.get(AWF_MCP_SERVER_KEY)
        if isinstance(candidate, Mapping):
            existing_entry = candidate
        elif AWF_MCP_SERVER_KEY in existing_servers:
            # An 'awf' entry that is not a table cannot be compared for drift and
            # would be silently overwritten by the merge; refuse the conflict.
            return ClientConfigPlan(
                client=client,
                method=method,
                config_path=config_path,
                action="conflict",
                conflict_detail=(
                    "An existing 'awf' MCP server entry is not a table; AWF refuses "
                    "to overwrite it. Resolve the entry manually, then re-run."
                ),
                desired_entry=desired_entry,
                cli_command=(
                    descriptor.add_command(env_file_str) if method == "official_cli" else None
                ),
                descriptor=descriptor,
            )

    file_missing = existing is None

    if existing_entry is not None and not _entries_match(existing_entry, desired_entry):
        return ClientConfigPlan(
            client=client,
            method=method,
            config_path=config_path,
            action="conflict",
            conflict_detail=(
                "An existing 'awf' MCP server entry uses a different command/args; "
                "AWF refuses to overwrite it. Resolve the entry manually, then re-run."
            ),
            desired_entry=desired_entry,
            existing_entry=existing_entry,
            cli_command=descriptor.add_command(env_file_str) if method == "official_cli" else None,
            descriptor=descriptor,
        )

    if existing_entry is not None:
        # Command/args already match; leave any auxiliary fields untouched.
        return ClientConfigPlan(
            client=client,
            method=method,
            config_path=config_path,
            action="no_change",
            desired_entry=desired_entry,
            existing_entry=existing_entry,
            cli_command=descriptor.add_command(env_file_str) if method == "official_cli" else None,
            descriptor=descriptor,
        )

    merged = _merged_config(existing, descriptor, desired_entry)
    try:
        existing_text = _serialize_config(existing or {}, descriptor.config_format)
        merged_text = _serialize_config(merged, descriptor.config_format)
    except _TomlEmitError as exc:
        # The scoped TOML emitter cannot round-trip the existing content safely,
        # so refuse rather than risk corrupting unrelated config.
        return ClientConfigPlan(
            client=client,
            method=method,
            config_path=config_path,
            action="conflict",
            conflict_detail=(
                "Existing Codex config contains values AWF cannot safely round-trip "
                f"({exc}); edit ~/.codex/config.toml manually or use the codex CLI."
            ),
            desired_entry=desired_entry,
            cli_command=descriptor.add_command(env_file_str) if method == "official_cli" else None,
            descriptor=descriptor,
        )

    action: ClientConfigAction = "create" if file_missing else "update"
    diff = _unified_diff(existing_text, merged_text, config_path)
    backup_path = (
        _backup_path(config_path, now()) if method == "file" and action == "update" else None
    )
    return ClientConfigPlan(
        client=client,
        method=method,
        config_path=config_path,
        action=action,
        diff=diff,
        backup_path=backup_path,
        desired_entry=desired_entry,
        merged_config=merged,
        cli_command=descriptor.add_command(env_file_str) if method == "official_cli" else None,
        descriptor=descriptor,
    )


def apply_client_config_plan(
    plan: ClientConfigPlan,
    *,
    run: CommandRunner,
    now: NowFn = lambda: datetime.now(UTC),
) -> ClientWriteResult:
    """Apply a client MCP integration plan (never called under dry-run).

    Refuses conflicts, no-ops on ``no_change``, prefers the official CLI when the
    plan selected it, else backs up the existing file before an atomic structured
    write. Raises reason-coded ``SetupCheckError`` for conflicts and write
    failures; never reads or handles provider tokens.
    """
    if plan.action == "conflict":
        raise SetupCheckError(
            "Refusing to write a conflicting client MCP configuration.",
            reason_code=CLIENT_CONFIG_CONFLICT,
            details=_conflict_details(plan),
        )
    if plan.action == "no_change":
        return ClientWriteResult(
            client=plan.client,
            method=plan.method,
            config_path=plan.config_path,
            action=plan.action,
            wrote=False,
        )
    if plan.method == "official_cli":
        return _apply_official_cli(plan, run=run)
    return _apply_file(plan, now=now)


def setup_client(
    client: str,
    *,
    env_file: str | Path,
    dry_run: bool,
    home: Path,
    which: WhichFn,
    run: CommandRunner,
    now: NowFn = lambda: datetime.now(UTC),
) -> FirstRunPayload:
    """Build (and, unless ``dry_run``, apply) the client MCP integration.

    Returns a reason-coded :class:`FirstRunPayload`: a success payload carrying
    the diff/planned action on dry-run or after a successful write, and a blocked
    payload for conflicts and write failures (folded via the T03 builders). No
    provider token is ever read, accepted, stored, or returned.
    """
    plan = build_client_config_plan(client, env_file=env_file, home=home, which=which, now=now)
    if dry_run:
        return _dry_run_payload(plan)
    return _apply_plan_payload(plan, run=run, now=now)


def setup_clients(
    clients: Sequence[str],
    *,
    env_file: str | Path,
    dry_run: bool,
    home: Path,
    which: WhichFn,
    run: CommandRunner,
    now: NowFn = lambda: datetime.now(UTC),
) -> list[FirstRunPayload]:
    """Plan every selected client first, then apply only if none conflict.

    Building all plans up front is safe because planning is side-effect free
    (:func:`build_client_config_plan` only reads). Applying each client inline
    instead would let one conflicting target leave earlier/later clients
    mutated while the run still reports blocked overall -- a partial write. So
    when any plan is a conflict (the failure mode that *is* knowable before any
    mutation), nothing is applied and each client renders a plan-only payload
    (a blocked conflict for the offender, an informational not-applied plan for
    the rest). Unpredictable apply-time failures (an official-CLI/IO error) are
    not pre-checkable, so a clean multi-client run still applies in order. On
    ``dry_run`` nothing is ever applied. Returns one payload per client, in the
    given order, for the caller to fold into a combined report.
    """
    plans = [
        build_client_config_plan(client, env_file=env_file, home=home, which=which, now=now)
        for client in clients
    ]
    if dry_run:
        return [_dry_run_payload(plan) for plan in plans]
    if any(plan.action == "conflict" for plan in plans):
        return [_plan_only_payload(plan) for plan in plans]
    return [_apply_plan_payload(plan, run=run, now=now) for plan in plans]


def _apply_plan_payload(
    plan: ClientConfigPlan, *, run: CommandRunner, now: NowFn
) -> FirstRunPayload:
    """Apply a non-dry-run plan and render its success/blocked payload."""
    try:
        result = apply_client_config_plan(plan, run=run, now=now)
    except SetupCheckError as error:
        return first_run_failure_payload(
            command=SETUP_COMMAND,
            reason_code=error.reason_code,
            status="blocked",
            summary=f"AWF could not configure the {_label(plan.client)} MCP client.",
            details=error.details,
            next_steps=_client_next_steps(error.reason_code),
        )
    return first_run_success_payload(
        command=SETUP_COMMAND,
        summary=_apply_summary(result),
        details=_apply_details(plan, result),
        next_steps=("Restart the client session to load the AWF MCP server.",),
    )


def _plan_only_payload(plan: ClientConfigPlan) -> FirstRunPayload:
    """Render a planned-but-not-applied payload for a conflict-blocked run.

    Used when a sibling client conflicts so the whole multi-client run applies
    nothing: the conflicting client renders its blocked conflict payload, while
    a clean create/update reports that it was planned but *not* written (so the
    combined report never implies a partial apply) and a no-change client
    reports it needs no change. ``dry_run`` is ``False`` in the details because
    nothing was applied for the *sibling conflict*, not because of a dry-run.
    """
    if plan.action == "conflict":
        return _dry_run_payload(plan)
    label = _label(plan.client)
    if plan.action == "no_change":
        summary = f"{label} MCP config already registers the AWF server; no change needed."
    else:
        verb = "create" if plan.action == "create" else "update"
        summary = (
            f"awf setup would {verb} the {label} MCP config but applied nothing "
            "because another selected client conflicts."
        )
    return first_run_success_payload(
        command=SETUP_COMMAND,
        summary=summary,
        details=_plan_details(plan, dry_run=False),
        next_steps=(
            "Resolve the conflicting client reported above, then re-run setup for these clients.",
        ),
    )


# --- read + compute helpers ----------------------------------------------


def _read_existing_config(
    config_path: Path,
    config_format: ClientConfigFormat,
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Return ``(parsed_config, parse_error)`` for the known client config path.

    ``parsed_config`` is ``None`` when the file is absent (a fresh ``create``).
    A read/parse failure or a non-mapping top-level document yields a non-secret
    ``parse_error`` string that the caller turns into an ambiguous ``conflict``.
    """
    if not config_path.exists():
        return None, None
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"Existing config could not be read ({type(exc).__name__})."
    try:
        if config_format == "json":
            parsed: object = json.loads(raw_text) if raw_text.strip() else {}
        else:
            parsed = tomllib.loads(raw_text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeError) as exc:
        return None, f"Existing config is not valid {config_format.upper()} ({type(exc).__name__})."
    if not isinstance(parsed, Mapping):
        return None, f"Existing {config_format.upper()} config is not a table/object."
    return parsed, None


def _entries_match(existing: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    """Return whether an existing server entry matches the desired command/args.

    The meaningful identity of an MCP server entry is its launch command and
    args; matching those means the client already points at AWF, so auxiliary
    fields (timeouts, ``type``) are left untouched rather than treated as drift.
    """
    return existing.get("command") == desired.get("command") and list(
        existing.get("args", [])
    ) == list(desired.get("args", []))


def _merged_config(
    existing: Mapping[str, Any] | None,
    descriptor: ClientDescriptor,
    desired_entry: Mapping[str, Any],
) -> dict[str, Any]:
    """Return ``existing`` with the AWF server entry added, preserving the rest."""
    merged = _deep_copy_mapping(existing) if existing is not None else {}
    servers = merged.get(descriptor.servers_key)
    servers_map: dict[str, Any] = dict(servers) if isinstance(servers, Mapping) else {}
    servers_map[AWF_MCP_SERVER_KEY] = dict(desired_entry)
    merged[descriptor.servers_key] = servers_map
    return merged


def _deep_copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a plain, deep-copied dict for safe merge mutation."""
    return {key: _deep_copy_value(item) for key, item in value.items()}


def _deep_copy_value(value: Any) -> Any:
    """Deep-copy nested mappings/sequences into plain dicts/lists."""
    if isinstance(value, Mapping):
        return _deep_copy_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_deep_copy_value(item) for item in value]
    return value


def _serialize_config(config: Mapping[str, Any], config_format: ClientConfigFormat) -> str:
    """Serialize a config mapping deterministically for diffing/writing."""
    if config_format == "json":
        return json.dumps(config, indent=2, sort_keys=True) + "\n"
    return _emit_toml(config)


def _unified_diff(existing_text: str, merged_text: str, config_path: Path) -> str:
    """Return a unified diff between the existing and merged config text."""
    diff_lines = unified_diff(
        existing_text.splitlines(keepends=True),
        merged_text.splitlines(keepends=True),
        fromfile=f"a/{config_path.name}",
        tofile=f"b/{config_path.name}",
    )
    return "".join(diff_lines)


def _backup_path(config_path: Path, now: datetime) -> Path:
    """Return the timestamped backup path for replacing ``config_path``."""
    stamp = now.strftime("%Y%m%d%H%M%S")
    return config_path.with_name(f"{config_path.name}.awf-backup-{stamp}")


# --- scoped TOML emitter --------------------------------------------------


def _emit_toml(config: Mapping[str, Any]) -> str:
    """Emit a minimal, deterministic TOML document for ``config``.

    Scoped to string/int/bool values and arrays of those, plus nested tables;
    any other value (float, datetime, array-of-tables, inline structure) raises
    :class:`_TomlEmitError` so the caller can refuse rather than corrupt config.
    """
    lines: list[str] = []
    _emit_toml_table(config, (), lines)
    text = "\n".join(lines)
    return text + "\n" if text else ""


def _emit_toml_table(table: Mapping[str, Any], path: tuple[str, ...], lines: list[str]) -> None:
    """Emit one TOML table (scalars first, then nested sub-tables)."""
    scalars = {k: v for k, v in table.items() if not isinstance(v, Mapping)}
    subtables = {k: v for k, v in table.items() if isinstance(v, Mapping)}
    if path:
        if lines:
            lines.append("")
        lines.append(f"[{'.'.join(_emit_toml_key(part) for part in path)}]")
    for key, value in scalars.items():
        lines.append(f"{_emit_toml_key(key)} = {_emit_toml_value(value)}")
    for key, value in subtables.items():
        _emit_toml_table(value, (*path, key), lines)


def _emit_toml_key(key: object) -> str:
    """Return a TOML key, quoting it when it is not a bare-key identifier."""
    key_text = str(key)
    if key_text and all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in key_text):
        return key_text
    return json.dumps(key_text)


def _emit_toml_value(value: Any) -> str:
    """Return the TOML literal for a scalar/array value, else raise."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_emit_toml_value(item) for item in value) + "]"
    raise _TomlEmitError(f"unsupported {type(value).__name__} value")


# --- apply helpers --------------------------------------------------------


def _apply_official_cli(plan: ClientConfigPlan, *, run: CommandRunner) -> ClientWriteResult:
    """Register the server via the official client CLI (no file write)."""
    command = plan.cli_command or ()
    result = run(command)
    if result is None or result.returncode != 0:
        raise SetupCheckError(
            "The official client CLI failed to register the AWF MCP server.",
            reason_code=CLIENT_CONFIG_WRITE_FAILED,
            details=_cli_failure_details(plan, result),
        )
    return ClientWriteResult(
        client=plan.client,
        method=plan.method,
        config_path=plan.config_path,
        action=plan.action,
        wrote=True,
    )


def _apply_file(plan: ClientConfigPlan, *, now: NowFn) -> ClientWriteResult:
    """Back up any existing file, then atomically write the merged config."""
    descriptor = plan.descriptor
    if descriptor is None or plan.merged_config is None:  # pragma: no cover - defensive
        raise SetupCheckError(
            "Incomplete client config plan for a file write.",
            reason_code=CLIENT_CONFIG_WRITE_FAILED,
            details={"client": plan.client, "config_path": str(plan.config_path)},
        )
    text = _serialize_config(plan.merged_config, descriptor.config_format)
    backup_path = _backup_path(plan.config_path, now()) if plan.action == "update" else None
    try:
        _write_config_atomic(plan.config_path, text, backup_path=backup_path)
    except OSError as exc:
        raise SetupCheckError(
            "Unable to write the client MCP configuration file.",
            reason_code=CLIENT_CONFIG_WRITE_FAILED,
            details={
                "client": plan.client,
                "config_path": str(plan.config_path),
                "error_type": type(exc).__name__,
            },
        ) from exc
    return ClientWriteResult(
        client=plan.client,
        method=plan.method,
        config_path=plan.config_path,
        action=plan.action,
        wrote=True,
        backup_path=backup_path,
    )


def _write_config_atomic(config_path: Path, text: str, *, backup_path: Path | None) -> None:
    """Atomically write ``text`` to ``config_path`` after an optional backup.

    Parent directories are created ``0o700`` and the file lands ``0o600`` (it may
    point at an env-file path, so keep it owner-private). The backup of an
    existing file is taken before the replacement so an interrupted run never
    destroys the prior config.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(config_path.parent, 0o700)
    if backup_path is not None and config_path.exists():
        backup_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
        _chmod_best_effort(backup_path, 0o600)
    tmp_path = config_path.with_name(f".{config_path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:  # pragma: no cover - defensive temp-file cleanup
        # The covered write-failure route is the mkdir/open ``OSError`` above; a
        # write/flush/fsync failure after a successful open is not portably
        # reproducible, so this only guarantees the temp inode is unlinked before
        # the error propagates to the reason-coded handler in ``_apply_file``.
        with suppress(OSError):
            tmp_path.unlink()
        raise
    _chmod_best_effort(tmp_path, 0o600)
    tmp_path.replace(config_path)
    _chmod_best_effort(config_path, 0o600)


def _chmod_best_effort(path: Path, mode: int) -> None:
    """Apply POSIX file permissions when supported by the host platform."""
    if os.name != "posix":  # pragma: no cover - non-POSIX platform branch
        return
    with suppress(OSError):
        path.chmod(mode)


# --- payload helpers ------------------------------------------------------


def _dry_run_payload(plan: ClientConfigPlan) -> FirstRunPayload:
    """Render a dry-run payload carrying the planned action and diff."""
    if plan.action == "conflict":
        return first_run_failure_payload(
            command=SETUP_COMMAND,
            reason_code=CLIENT_CONFIG_CONFLICT,
            status="blocked",
            summary=f"AWF found a {_label(plan.client)} MCP config conflict.",
            details=_conflict_details(plan),
            next_steps=_client_next_steps(CLIENT_CONFIG_CONFLICT),
        )
    return first_run_success_payload(
        command=SETUP_COMMAND,
        summary=_dry_run_summary(plan),
        details=_plan_details(plan, dry_run=True),
        next_steps=(
            f"Re-run `awf setup --client {plan.client}` without --dry-run to apply this change.",
        ),
    )


def _plan_details(plan: ClientConfigPlan, *, dry_run: bool) -> dict[str, Any]:
    """Return non-secret plan details for a rendered payload."""
    details: dict[str, Any] = {
        "client": plan.client,
        "config_path": str(plan.config_path),
        "action": plan.action,
        "method": plan.method,
        "dry_run": dry_run,
    }
    if plan.diff:
        details["diff"] = plan.diff
    if plan.backup_path is not None:
        details["backup_path"] = str(plan.backup_path)
    return details


def _apply_details(plan: ClientConfigPlan, result: ClientWriteResult) -> dict[str, Any]:
    """Return non-secret details for a successful apply payload."""
    details: dict[str, Any] = {
        "client": result.client,
        "config_path": str(result.config_path),
        "action": result.action,
        "method": result.method,
        "wrote": result.wrote,
    }
    if plan.diff:
        details["diff"] = plan.diff
    if result.backup_path is not None:
        details["backup_path"] = str(result.backup_path)
    return details


def _conflict_details(plan: ClientConfigPlan) -> dict[str, Any]:
    """Return non-secret diagnostic details for a conflict."""
    details: dict[str, Any] = {
        "client": plan.client,
        "config_path": str(plan.config_path),
        "action": plan.action,
    }
    if plan.conflict_detail is not None:
        details["conflict_detail"] = plan.conflict_detail
    return details


def _cli_failure_details(plan: ClientConfigPlan, result: CommandResult | None) -> dict[str, Any]:
    """Return non-secret diagnostic details for an official-CLI failure."""
    details: dict[str, Any] = {
        "client": plan.client,
        "method": plan.method,
        "cli_binary": plan.descriptor.cli_binary if plan.descriptor else plan.client,
    }
    if result is None:
        details["error"] = "the client CLI could not be launched"
    else:
        details["returncode"] = result.returncode
        stderr = result.stderr.strip()
        if stderr:
            details["stderr"] = stderr
    return details


def _dry_run_summary(plan: ClientConfigPlan) -> str:
    """Return the dry-run summary line for a non-conflict plan."""
    label = _label(plan.client)
    if plan.action == "no_change":
        return f"{label} MCP config already registers the AWF server; no change needed."
    verb = "create" if plan.action == "create" else "update"
    return f"awf setup --client {plan.client} would {verb} the {label} MCP config (dry-run)."


def _apply_summary(result: ClientWriteResult) -> str:
    """Return the success summary line for an applied plan."""
    label = _label(result.client)
    if result.action == "no_change":
        return f"{label} MCP config already registers the AWF server; no change needed."
    return f"AWF registered its MCP server in the {label} client config."


def _client_next_steps(reason_code: str) -> tuple[str, ...]:
    """Return operator next-step guidance for a reason-coded client failure."""
    if reason_code == CLIENT_CONFIG_CONFLICT:
        return (
            "Review the existing client config or the proposed diff, resolve the "
            "conflicting 'awf' server entry, then re-run setup for that client.",
        )
    if reason_code == CLIENT_CONFIG_WRITE_FAILED:
        return (
            "Check the client config file permissions and parent directories, "
            "preserve any backup, then re-run setup for that client.",
        )
    # Defensive fallback: ``setup_client`` only routes the two client reason codes
    # above here, so this generic guidance is never reached via the public API.
    return (  # pragma: no cover - defensive fallback for unexpected reason codes
        "Fix the reported issue above, then re-run awf setup --client <client>.",
    )


def _label(client: str) -> str:
    """Return the display label for a known client key."""
    descriptor = CLIENT_DESCRIPTORS.get(client)
    return descriptor.label if descriptor is not None else client


def default_client_command_runner(args: Sequence[str]) -> CommandResult | None:
    """Run a bounded official-client probe, returning ``None`` when it cannot launch."""
    import subprocess

    try:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


__all__ = [
    "AWF_MCP_SERVER_KEY",
    "CLIENT_DESCRIPTORS",
    "KNOWN_SETUP_CLIENTS",
    "ClientConfigAction",
    "ClientConfigPlan",
    "ClientDescriptor",
    "ClientWriteResult",
    "apply_client_config_plan",
    "build_client_config_plan",
    "default_client_command_runner",
    "normalize_client",
    "normalize_clients",
    "setup_client",
    "setup_clients",
]
