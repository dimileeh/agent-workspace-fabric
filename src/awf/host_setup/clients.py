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
When the official client CLI is present *and* it can register the full desired
entry it is preferred for the apply (Claude Code); otherwise -- a missing CLI, or
Codex, whose ``mcp add`` cannot persist AWF's bounded startup/tool timeouts -- the
helpers fall back to structured JSON/TOML parsing, write a timestamped backup
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
    # An environment variable that, when set, overrides the *directory* the
    # client reads/writes its config under (Codex honors ``CODEX_HOME`` over
    # ``~/.codex``). ``None`` for clients with no such override (Claude Code).
    config_home_env: str | None = None
    # Whether the client's official ``mcp add`` CLI registers the *full* desired
    # entry. When ``False`` the CLI drops fields AWF requires (Codex's ``mcp
    # add`` constructs the server with ``startup_timeout_sec``/``tool_timeout_sec``
    # left unset, ignoring the bounded timeouts in :meth:`desired_entry`), so the
    # structured file write is used even when the CLI is on ``PATH`` — otherwise
    # the applied config would silently diverge from the dry-run/file plan.
    cli_applies_full_entry: bool = True
    # Entry fields beyond command/args that AWF must persist exactly. An existing
    # entry whose command/args match but that is missing or stale on one of these
    # (Codex's bounded ``startup_timeout_sec``/``tool_timeout_sec``, which ``codex
    # mcp add`` and pre-file-write AWF setups leave unset; Claude's stdio
    # transport ``type``, which a stale/malformed entry may set to HTTP/SSE) is
    # refreshed via an ``update`` rather than reported as a ``no_change`` that
    # leaves it broken.
    required_entry_fields: tuple[str, ...] = ()

    def config_path(self, home: Path, env: Mapping[str, str]) -> Path:
        """Return the absolute client config path under ``home``.

        When ``config_home_env`` is set and that variable holds a non-empty
        value, the client (e.g. Codex via ``CODEX_HOME``) reads and writes its
        config under that directory — and its official CLI mutates the file
        there — so the planned/written path must follow the override rather than
        the hard-coded ``home``-anchored path the client would never load. The
        override replaces the first relative segment (the home-anchored config
        dir), keeping the config filename. A leading ``~``/``~user`` in the
        override is expanded (mirroring ``home`` resolution) so a
        ``CODEX_HOME=~/.codex`` set outside a shell does not target a literal
        ``~`` directory the client never loads.
        """
        if self.config_home_env is not None:
            override = env.get(self.config_home_env, "").strip()
            if override:
                return Path(override).expanduser().joinpath(*self.config_relative_path[1:])
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
        """Return the official-CLI argv that registers the AWF MCP server.

        Only clients whose official CLI applies the full desired entry
        (``cli_applies_full_entry``) reach this path, so today this is the Claude
        Code registration command alone. Codex's ``mcp add`` cannot persist the
        bounded startup/tool timeouts AWF requires, so Codex always uses the
        structured file write and never builds an official-CLI command.
        """
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


CLIENT_DESCRIPTORS: Mapping[str, ClientDescriptor] = {
    "claude": ClientDescriptor(
        key="claude",
        label="Claude Code",
        config_format="json",
        cli_binary="claude",
        config_relative_path=(".claude.json",),
        servers_key="mcpServers",
        # Claude's JSON ``type`` field selects the transport (stdio vs HTTP/SSE).
        # An entry with matching command/args but a stale/malformed non-stdio
        # ``type`` would never launch ``awf mcp serve``, so it is required exactly
        # and refreshed via an update rather than reported as a no-op.
        required_entry_fields=("type",),
    ),
    "codex": ClientDescriptor(
        key="codex",
        label="Codex",
        config_format="toml",
        cli_binary="codex",
        config_relative_path=(".codex", "config.toml"),
        servers_key="mcp_servers",
        config_home_env="CODEX_HOME",
        # Codex's ``mcp add`` ignores the bounded startup/tool timeouts AWF
        # registers, so write the config file directly to honor them.
        cli_applies_full_entry=False,
        # These bounded timeouts must be present on a matching entry; an entry
        # created without them is refreshed rather than treated as a no-op.
        required_entry_fields=("startup_timeout_sec", "tool_timeout_sec"),
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
    # Set when the scoped diff filter dropped a genuine AWF line that
    # coincidentally matched existing content, so the preview is incomplete
    # (the write itself is always exact). Mirrors the ``official_cli`` flag.
    diff_is_approximate: bool = False
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
    env: Mapping[str, str] = os.environ,
) -> ClientConfigPlan:
    """Compute a read-only plan for registering AWF's MCP server into ``client``.

    Reads only the single known client config path (no home-dir scanning) via a
    structured parse; an unparseable or non-mapping existing config is an
    ambiguous ``conflict`` that refuses to write. The env-file path is used only
    as a string; the file's contents are never read. ``env`` resolves any
    home-override variable (e.g. Codex's ``CODEX_HOME``) so the planned path
    matches the file the client and its official CLI actually load.
    """
    descriptor = CLIENT_DESCRIPTORS[client]
    config_path = descriptor.config_path(home, env)
    env_file_str = str(env_file)
    desired_entry = descriptor.desired_entry(env_file_str)
    # Prefer the official CLI only when it can apply the full desired entry; Codex's
    # ``mcp add`` drops the bounded timeouts, so it always takes the file path.
    method: ClientConfigMethod = (
        "official_cli"
        if descriptor.cli_applies_full_entry and which(descriptor.cli_binary)
        else "file"
    )

    existing, parse_error, existing_has_comments = _read_existing_config(
        config_path, descriptor.config_format
    )
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
            conflict_detail=_command_args_conflict_detail(existing_entry, desired_entry),
            desired_entry=desired_entry,
            existing_entry=existing_entry,
            cli_command=descriptor.add_command(env_file_str) if method == "official_cli" else None,
            descriptor=descriptor,
        )

    if existing_entry is not None and _entry_has_required_fields(
        existing_entry, desired_entry, descriptor.required_entry_fields
    ):
        # Command/args match and every required field (e.g. Codex's bounded
        # startup/tool timeouts) is already present and current; leave the rest
        # of the entry untouched.
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
    # Either no existing entry (create) or an entry whose command/args match but
    # that lacks a required field — a Codex entry written by ``codex mcp add`` or
    # a pre-file-write AWF setup carries no bounded timeouts. Fall through to an
    # ``update`` so the file write adds them instead of a misleading no-op.

    if existing is not None and existing_has_comments:
        # ``tomllib`` dropped the file's comments on parse, so rewriting it would
        # delete hand-written documentation the comment-free scoped diff cannot
        # surface (the dry-run would look like a pure AWF addition). Refuse rather
        # than silently destroy unrelated content — mirroring the other
        # non-round-trippable TOML refusals above.
        return ClientConfigPlan(
            client=client,
            method=method,
            config_path=config_path,
            action="conflict",
            conflict_detail=(
                f"Existing {_label(client)} config at {config_path} contains comments that "
                "AWF cannot preserve when rewriting it; the structured write would drop them. "
                "Remove the comments or add the 'awf' MCP server entry manually, then re-run."
            ),
            desired_entry=desired_entry,
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
                f"({exc}); edit {config_path} manually or use the codex CLI."
            ),
            desired_entry=desired_entry,
            cli_command=descriptor.add_command(env_file_str) if method == "official_cli" else None,
            descriptor=descriptor,
        )

    action: ClientConfigAction = "create" if file_missing else "update"
    diff, diff_is_approximate = _unified_diff(
        existing_text, merged_text, config_path, replacing_entry=existing_entry is not None
    )
    backup_path = (
        _backup_path(config_path, now()) if method == "file" and action == "update" else None
    )
    return ClientConfigPlan(
        client=client,
        method=method,
        config_path=config_path,
        action=action,
        diff=diff,
        diff_is_approximate=diff_is_approximate,
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
) -> ClientWriteResult:
    """Apply a client MCP integration plan (never called under dry-run).

    Refuses conflicts, no-ops on ``no_change``, prefers the official CLI when the
    plan selected it, else backs up the existing file before an atomic structured
    write. The backup path is taken from the plan (stamped once at plan time), so
    a dry-run preview never diverges from the file actually written. Raises
    reason-coded ``SetupCheckError`` for conflicts and write failures; never reads
    or handles provider tokens.
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
    return _apply_file(plan)


def setup_client(
    client: str,
    *,
    env_file: str | Path,
    dry_run: bool,
    home: Path,
    which: WhichFn,
    run: CommandRunner,
    now: NowFn = lambda: datetime.now(UTC),
    env: Mapping[str, str] = os.environ,
) -> FirstRunPayload:
    """Build (and, unless ``dry_run``, apply) the client MCP integration.

    Returns a reason-coded :class:`FirstRunPayload`: a success payload carrying
    the diff/planned action on dry-run or after a successful write, and a blocked
    payload for conflicts and write failures (folded via the T03 builders). No
    provider token is ever read, accepted, stored, or returned.
    """
    plan = build_client_config_plan(
        client, env_file=env_file, home=home, which=which, now=now, env=env
    )
    if dry_run:
        return _dry_run_payload(plan)
    return _apply_plan_payload(plan, run=run)


def setup_clients(
    clients: Sequence[str],
    *,
    env_file: str | Path,
    dry_run: bool,
    home: Path,
    which: WhichFn,
    run: CommandRunner,
    now: NowFn = lambda: datetime.now(UTC),
    env: Mapping[str, str] = os.environ,
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
        build_client_config_plan(
            client, env_file=env_file, home=home, which=which, now=now, env=env
        )
        for client in clients
    ]
    if dry_run:
        return [_dry_run_payload(plan) for plan in plans]
    if any(plan.action == "conflict" for plan in plans):
        return [_plan_only_payload(plan) for plan in plans]
    return [_apply_plan_payload(plan, run=run) for plan in plans]


def _apply_plan_payload(plan: ClientConfigPlan, *, run: CommandRunner) -> FirstRunPayload:
    """Apply a non-dry-run plan and render its success/blocked payload."""
    try:
        result = apply_client_config_plan(plan, run=run)
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
        # A no_change client is already correctly configured, so re-running setup
        # for it is a pointless no-op; only the conflicting sibling needs another run.
        next_steps = (
            "This client is already correctly configured; only re-run setup for the "
            "conflicting client reported above.",
        )
    else:
        verb = "create" if plan.action == "create" else "update"
        summary = (
            f"awf setup would {verb} the {label} MCP config but applied nothing "
            "because another selected client conflicts."
        )
        next_steps = (
            "Resolve the conflicting client reported above, then re-run setup for these clients.",
        )
    return first_run_success_payload(
        command=SETUP_COMMAND,
        summary=summary,
        details=_plan_details(plan, dry_run=False),
        next_steps=next_steps,
    )


# --- read + compute helpers ----------------------------------------------


class _DuplicateJsonKeyError(ValueError):
    """Raised when a JSON object contains a duplicate key.

    ``json.loads`` silently keeps only the *last* value for a repeated key, so a
    hand-edited ``~/.claude.json`` with duplicate top-level keys (e.g. two
    ``mcpServers`` blocks) would be parsed into a reduced mapping; rewriting that
    via ``_apply_file`` would delete the earlier block without surfacing a
    conflict or showing the deletion in the diff. TOML already rejects duplicate
    keys, so this restores parity by treating duplicates as ambiguous config.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"duplicate key {key!r}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that rejects duplicate keys in any JSON object."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _read_existing_config(
    config_path: Path,
    config_format: ClientConfigFormat,
) -> tuple[Mapping[str, Any] | None, str | None, bool]:
    """Return ``(parsed_config, parse_error, has_comments)`` for the config path.

    ``parsed_config`` is ``None`` when the file is absent (a fresh ``create``).
    A read/parse failure or a non-mapping top-level document yields a non-secret
    ``parse_error`` string that the caller turns into an ambiguous ``conflict``.
    Duplicate JSON object keys are likewise refused (see
    :class:`_DuplicateJsonKeyError`) so a rewrite never silently drops a block.
    ``has_comments`` reports whether a TOML document carries comments that
    ``tomllib`` discards on parse — the caller refuses to rewrite such a file
    rather than silently drop hand-written documentation (JSON has no comments,
    so it is always ``False`` there).
    """
    if not config_path.exists():
        return None, None, False
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"Existing config could not be read ({type(exc).__name__}).", False
    try:
        if config_format == "json":
            parsed: object = (
                json.loads(raw_text, object_pairs_hook=_reject_duplicate_json_keys)
                if raw_text.strip()
                else {}
            )
        else:
            parsed = tomllib.loads(raw_text)
    except _DuplicateJsonKeyError as exc:
        return (
            None,
            (
                f"Existing {config_format.upper()} config has a duplicate '{exc.key}' key; "
                "AWF refuses to rewrite it because the duplicate would be silently dropped. "
                "Resolve it manually, then re-run."
            ),
            False,
        )
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeError) as exc:
        return (
            None,
            f"Existing config is not valid {config_format.upper()} ({type(exc).__name__}).",
            False,
        )
    if not isinstance(parsed, Mapping):
        return None, f"Existing {config_format.upper()} config is not a table/object.", False
    has_comments = config_format == "toml" and _toml_text_has_comment(raw_text)
    return parsed, None, has_comments


def _toml_text_has_comment(raw_text: str) -> bool:
    """Return whether raw TOML text contains a comment outside any string.

    ``tomllib`` silently discards comments, so an ``update`` computed from the
    parsed document and written with the scoped emitter would drop hand-written
    documentation while the scoped diff — itself built from the comment-free
    re-serialization — never shows the loss. Detecting a real comment (a ``#``
    that is not inside a string) lets the planner refuse rather than destroy
    unrelated content. The scan tracks single-line basic/literal strings and
    triple-quoted multiline strings so a ``#`` inside a string value is not
    mistaken for a comment; an unterminated string is treated as running to the
    end of input, which can only over-detect (a safe refusal), never miss one.
    """
    i = 0
    length = len(raw_text)
    while i < length:
        if raw_text.startswith('"""', i) or raw_text.startswith("'''", i):
            delimiter = raw_text[i : i + 3]
            end = raw_text.find(delimiter, i + 3)
            i = length if end == -1 else end + 3
            continue
        char = raw_text[i]
        if char == "#":
            return True
        if char in ('"', "'"):
            i += 1
            while i < length and raw_text[i] not in (char, "\n"):
                # Basic strings honor backslash escapes; literal ('') strings do not.
                if char == '"' and raw_text[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        i += 1
    return False


def _entries_match(existing: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    """Return whether an existing server entry matches the desired command/args.

    The meaningful identity of an MCP server entry is its launch command and
    args; matching those means the client already points at AWF, so a difference
    here is *not* a drift conflict. Required auxiliary fields the planner must
    still persist (Codex's bounded timeouts, Claude's stdio transport ``type``)
    are checked separately by :func:`_entry_has_required_fields`; any remaining
    user-added fields are left untouched.

    An existing ``args`` that is not a list (e.g. Claude JSON ``"args": null``
    or Codex TOML ``args = 1``) cannot match the desired list args, so it
    reports "no match" — routing the entry to the reason-coded conflict path
    instead of crashing on ``list()`` of a non-iterable.
    """
    if existing.get("command") != desired.get("command"):
        return False
    existing_args = existing.get("args", [])
    desired_args = desired.get("args", [])
    if not isinstance(existing_args, (list, tuple)) or not isinstance(desired_args, (list, tuple)):
        return False
    return list(existing_args) == list(desired_args)


def _command_args_conflict_detail(existing: Mapping[str, Any], desired: Mapping[str, Any]) -> str:
    """Return the conflict detail for an ``awf`` entry whose command/args drifted.

    The base message names the generic "different command/args" drift. When the
    registered ``command`` is unchanged and only the (list) ``args`` differ —
    the common case after relocating the AWF install, where just the
    ``--env-file`` path moved — a diagnostic hint is appended so the operator
    knows to update the registered path rather than suspecting a wholly
    different binary. Non-list ``args`` (a malformed ``null``/scalar entry) skip
    the hint since the path framing would be misleading there.
    """
    detail = "An existing 'awf' MCP server entry uses a different command/args; "
    same_command = existing.get("command") == desired.get("command")
    if same_command and isinstance(existing.get("args"), (list, tuple)):
        detail += (
            "the registered command is unchanged and only its args differ "
            "(commonly a stale --env-file path after the AWF install moved). "
        )
    detail += "AWF refuses to overwrite it. Resolve the entry manually, then re-run."
    return detail


def _entry_has_required_fields(
    existing: Mapping[str, Any],
    desired: Mapping[str, Any],
    required_fields: tuple[str, ...],
) -> bool:
    """Return whether ``existing`` already carries every required desired field.

    ``_entries_match`` compares only command/args, but some clients need extra
    fields persisted exactly — Codex's bounded
    ``startup_timeout_sec``/``tool_timeout_sec`` and Claude's stdio transport
    ``type``. An ``awf`` entry created by ``codex mcp add`` (or by an AWF setup
    predating the direct file-write path) has the right command/args yet lacks
    those bounded timeouts; a Claude entry with a stale/malformed non-stdio
    ``type`` likewise has matching command/args but would not launch. Reporting
    either as a no-op would leave it broken, so this returns ``False`` for such
    an entry and the planner routes it to ``update`` so the file write fixes it.
    """
    return all(existing.get(field) == desired.get(field) for field in required_fields)


def _merged_config(
    existing: Mapping[str, Any] | None,
    descriptor: ClientDescriptor,
    desired_entry: Mapping[str, Any],
) -> dict[str, Any]:
    """Return ``existing`` with the AWF server entry added, preserving the rest."""
    merged = _deep_copy_mapping(existing) if existing is not None else {}
    servers = merged.get(descriptor.servers_key)
    servers_map: dict[str, Any] = dict(servers) if isinstance(servers, Mapping) else {}
    # AWF fully owns its own server entry: replace it wholesale rather than
    # deep-merging user-added fields into it. This guarantees the canonical
    # command/args and the bounded startup/tool timeouts are exactly what AWF
    # intends, with no stale or conflicting fields surviving an update. Any
    # user-added fields on the prior ``awf`` entry are dropped — their removed
    # lines echo existing content and are filtered from the scoped diff, so the
    # dry-run flags the diff approximate (see ``_hides_changed_line``) to surface
    # the loss, and the timestamped backup allows a rollback.
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
    """Serialize a config mapping deterministically for diffing/writing.

    Existing keys keep their on-disk order (insertion order is preserved through
    ``_merged_config``'s deep copy), so an update mutates only the ``awf`` entry
    and never reorders the rest of the file — consistent with the scoped-diff
    design in ``_unified_diff``. Determinism comes from the deterministic input
    ordering, not from ``sort_keys``.
    """
    if config_format == "json":
        return json.dumps(config, indent=2) + "\n"
    return _emit_toml(config)


def _unified_diff(
    existing_text: str,
    merged_text: str,
    config_path: Path,
    *,
    replacing_entry: bool,
) -> tuple[str, bool]:
    """Return the scoped unified diff and whether it omits genuine AWF lines.

    Emitted with zero context lines (``n=0``) and then filtered so the result
    contains only brand-new AWF lines. AWF only ever adds or changes its own
    ``awf`` server entry, so the changed lines should be exclusively AWF's. Two
    leakage channels are closed here:

    * Surrounding context — eliminated by ``n=0`` — would otherwise expose
      unrelated existing config (other servers' env entries or API keys the
      payload redactor may not match) in CLI output and logs.
    * A *pre-existing* line can still surface as a changed line when appending
      AWF's entry rewrites its syntax: in JSON the previous last key gains a
      trailing comma, so an existing ``{ "apiKey": "secret" }`` emits the
      ``apiKey`` line as both a removal and a comma-suffixed addition. Any diff
      body line equal (ignoring a trailing comma) to a line already present in
      ``existing_text`` is therefore suppressed — it is pre-existing user
      content, never an AWF addition — keeping those third-party secrets out of
      dry-run/apply payloads entirely.

    The second leakage guard is content-based, so it can also drop a *genuine*
    changed line whose serialized form coincidentally equals a line already in
    the file. This happens for both additions (another ``stdio`` server makes
    ``"type": "stdio"`` pre-exist) and removals — an ``update`` that replaces the
    whole ``awf`` entry deletes a user-added field (e.g. an ``env`` subtable)
    whose lines, being prior content, all echo the existing file and are
    suppressed wholesale. Either kind of drop leaves the preview incomplete, so
    this returns ``True`` as the second value to flag the diff approximate —
    mirroring the ``official_cli`` path — without affecting the always-exact
    write. The benign intended case is the trailing-comma rewrite of the prior
    last entry, where a suppressed addition is exactly offset by a suppressed
    removal of the same content; those cancel out and do not set the flag.

    A suppressed removal only signals a dropped field when ``replacing_entry`` is
    true (an existing ``awf`` entry is being replaced). With no prior entry the
    merge adds and never deletes AWF content, so an unpaired removal is structural
    noise (e.g. an empty ``{}`` document expanding into a populated one) and must
    not flag the create preview approximate.
    """
    existing_lines = {_normalize_config_line(line) for line in existing_text.splitlines()}
    kept: list[str] = []
    suppressed_additions: list[str] = []
    suppressed_removals: list[str] = []
    for line in unified_diff(
        existing_text.splitlines(keepends=True),
        merged_text.splitlines(keepends=True),
        fromfile=f"a/{config_path.name}",
        tofile=f"b/{config_path.name}",
        n=0,
    ):
        if not _echoes_existing_line(line, existing_lines):
            kept.append(line)
            continue
        bucket = suppressed_additions if line.startswith("+") else suppressed_removals
        bucket.append(_normalize_config_line(line[1:]))
    return "".join(kept), _hides_changed_line(
        suppressed_additions, suppressed_removals, replacing_entry=replacing_entry
    )


def _hides_changed_line(
    suppressed_additions: list[str],
    suppressed_removals: list[str],
    *,
    replacing_entry: bool,
) -> bool:
    """Return whether the filter dropped a genuine changed line, not just a comma rewrite.

    The benign case is the trailing-comma rewrite of the prior last entry: a
    suppressed addition paired with a suppressed removal of the same content.
    Those pairs cancel out. Any *leftover* suppressed line is a genuine change the
    preview now hides:

    * an unpaired addition is a brand-new AWF line that coincidentally matched
      existing content, so the preview understates what is added;
    * an unpaired removal, *when an existing entry is being replaced*
      (``replacing_entry``), is a field being deleted — an ``update`` that
      replaces the whole ``awf`` entry drops a user-added field (e.g. an ``env``
      subtable), so the preview would otherwise hide a destructive change.

    When no prior entry is replaced, unpaired removals are structural artifacts of
    serialization (an empty ``{}`` document becoming populated), not dropped
    fields, so they do not flag the preview. Either flagged case leaves the
    preview incomplete, so the diff is marked approximate.
    """
    remaining = list(suppressed_removals)
    for added in suppressed_additions:
        if added in remaining:
            remaining.remove(added)
        else:
            return True
    return replacing_entry and bool(remaining)


def _normalize_config_line(body: str) -> str:
    """Normalize a config line for pre-existing comparison (drop a trailing comma).

    Appending a key in JSON gives the previous last line a structural trailing
    comma; stripping a single trailing comma lets that rewritten line match its
    original form. Internal commas inside a string value sit before the closing
    quote, so they are untouched.
    """
    return body.rstrip().removesuffix(",").rstrip()


def _echoes_existing_line(line: str, existing_lines: set[str]) -> bool:
    """Return whether a unified-diff line merely echoes a pre-existing config line.

    File headers (``---``/``+++``) and hunk headers are never suppressed; only
    ``+``/``-`` body lines whose content already exists in the original file
    (ignoring a trailing comma added when AWF's entry is appended) are dropped.
    """
    if line.startswith(("---", "+++")) or not line.startswith(("+", "-")):
        return False
    return _normalize_config_line(line[1:]) in existing_lines


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
    """Emit one TOML table (scalars first, then nested sub-tables).

    A header is emitted for a non-root table only when it carries scalars or is a
    genuine leaf (no sub-tables). A pure container node — sub-tables but no scalars,
    e.g. ``mcp_servers`` holding only ``[mcp_servers.awf]``/``[mcp_servers.other]``
    — is skipped: its own ``[mcp_servers]`` header would be a redundant round-trip
    artifact that surfaces as a spurious ``+[mcp_servers]`` line in the dry-run
    diff. Skipping ``not subtables`` rather than ``scalars`` alone still preserves a
    deliberately empty leaf table, whose only representation is its header.
    """
    scalars = {k: v for k, v in table.items() if not isinstance(v, Mapping)}
    subtables = {k: v for k, v in table.items() if isinstance(v, Mapping)}
    if path and (scalars or not subtables):
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


def _apply_file(plan: ClientConfigPlan) -> ClientWriteResult:
    """Back up any existing file, then atomically write the merged config."""
    descriptor = plan.descriptor
    if descriptor is None or plan.merged_config is None:  # pragma: no cover - defensive
        raise SetupCheckError(
            "Incomplete client config plan for a file write.",
            reason_code=CLIENT_CONFIG_WRITE_FAILED,
            details={"client": plan.client, "config_path": str(plan.config_path)},
        )
    text = _serialize_config(plan.merged_config, descriptor.config_format)
    # Reuse the plan's already-stamped backup path rather than re-stamping with a
    # fresh now(): a second boundary between plan and apply would otherwise make
    # the dry-run path (from _plan_details) diverge from the file on disk.
    backup_path = plan.backup_path if plan.action == "update" else None
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

    A client config dir AWF creates is tightened to ``0o700`` and the file lands
    ``0o600`` (it may point at an env-file path, so keep it owner-private). An
    already-existing parent is left untouched: for Claude's file fallback the
    parent is ``$HOME``, so chmodding it would clobber intentionally shared
    home-directory permissions. The backup of an existing file is taken before
    the replacement so an interrupted run never destroys the prior config.

    When ``config_path`` is a symlink (e.g. a dotfiles-managed ``~/.claude.json``
    or ``$CODEX_HOME/config.toml``), the atomic replace is targeted at the real
    file the link resolves to. ``Path.replace`` renames over the link path
    itself, which would otherwise destroy the symlink and strand the user's
    managed config; resolving first updates that managed file in place and keeps
    the link intact.
    """
    target_path = config_path.resolve() if config_path.is_symlink() else config_path
    parent_existed = target_path.parent.exists()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        _chmod_best_effort(target_path.parent, 0o700)
    if backup_path is not None and target_path.exists():
        # Create the backup owner-private from the start: it is a verbatim copy of
        # the prior config (which may hold API keys/tokens from other entries), so
        # it must never briefly exist at umask perms. ``Path.write_text`` would
        # create it world-readable and rely on a best-effort chmod that silently
        # swallows OSError; ``os.open(..., 0o600)`` matches the temp-file write
        # below and refuses to follow a pre-existing path (O_EXCL).
        backup_text = target_path.read_text(encoding="utf-8")
        backup_fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            backup_handle = os.fdopen(backup_fd, "w", encoding="utf-8")
        except Exception:  # pragma: no cover - defensive: fdopen failure not portably reproducible
            # ``fdopen`` never took ownership of ``backup_fd``, so close it ourselves
            # before unlinking to avoid leaking the descriptor — mirroring the
            # temp-file open guard below.
            with suppress(OSError):
                os.close(backup_fd)
            with suppress(OSError):
                backup_path.unlink()
            raise
        try:
            with backup_handle:
                backup_handle.write(backup_text)
        except Exception:  # pragma: no cover - defensive backup cleanup
            with suppress(OSError):
                backup_path.unlink()
            raise
    tmp_path = target_path.with_name(f".{target_path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:  # pragma: no cover - defensive: fdopen failure not portably reproducible
        # ``fdopen`` never took ownership of ``fd`` (e.g. MemoryError), so close
        # it ourselves before unlinking to avoid leaking the descriptor. This is
        # scoped to the fdopen-failure case only: once ``fdopen`` succeeds the
        # ``with`` below owns and closes ``fd``, so closing it here would risk
        # double-closing a since-reused descriptor number.
        with suppress(OSError):
            os.close(fd)
        with suppress(OSError):
            tmp_path.unlink()
        raise
    try:
        with handle:
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
    tmp_path.replace(target_path)
    _chmod_best_effort(target_path, 0o600)


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
    if plan.action == "no_change":
        next_steps: tuple[str, ...] = (
            f"The {_label(plan.client)} MCP client already registers the AWF server; "
            "no action needed.",
        )
    else:
        next_steps = (
            f"Re-run `awf setup --client {plan.client}` without --dry-run to apply this change.",
        )
    return first_run_success_payload(
        command=SETUP_COMMAND,
        summary=_dry_run_summary(plan),
        details=_plan_details(plan, dry_run=True),
        next_steps=next_steps,
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
    if plan.method == "official_cli" and plan.action in ("create", "update"):
        # The diff is computed from a file-based merge, but the apply shells out to
        # the client's own ``mcp add`` CLI, whose JSON formatting/field set may
        # differ from this preview. Mark the diff approximate and surface the exact
        # argv that will run so an operator approving a dry-run is not misled into
        # thinking the preview byte-for-byte describes the resulting file.
        details["diff_is_approximate"] = True
        details["cli_command"] = list(plan.cli_command or ())
    elif plan.diff_is_approximate:
        # The file path is exact on write, but the scoped diff filter dropped a
        # genuine changed line that coincidentally matched existing content —
        # either an added line that understates what is written, or a removed
        # line for a dropped field that understates what is deleted. Flag it the
        # same way so the operator knows the diff is not the full picture.
        details["diff_is_approximate"] = True
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
