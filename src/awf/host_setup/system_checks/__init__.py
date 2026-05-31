"""Read-only host system readiness checks for ``awf setup``.

This package probes whether the local machine can run AWF Core **without
starting Core and without touching secrets**. Every probe is bounded, uses the
standard library only, and catches specific exceptions (never bare ``except``,
never a hidden retry). Subprocess/socket/filesystem dependencies are injected so
the checks are fully hermetic under test.

The individual probes live in focused submodules
(:mod:`~awf.host_setup.system_checks.primitives`,
:mod:`~awf.host_setup.system_checks.checks_core`,
:mod:`~awf.host_setup.system_checks.checks_ports`,
:mod:`~awf.host_setup.system_checks.checks_host`); this module aggregates them
via :func:`run_system_checks`, validates provider selectors, and renders the
first-run readiness payload. The public surface re-exported here is unchanged
from the pre-decomposition module.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

# The reason-code constants below are rendering-layer contracts owned by
# ``awf.host_setup.rendering``. They are imported here purely for internal use
# (raised by ``normalize_provider``/``require_interactive`` and attached to
# readiness issues) and are deliberately NOT re-exported via ``__all__`` so
# ``rendering`` stays their single canonical public import path.
from awf.host_setup.rendering import (
    INTERACTIVE_INPUT_REQUIRED,
    SETUP_PROVIDER_UNKNOWN,
    SETUP_READINESS_FAILED,
    FirstRunIssue,
    FirstRunPayload,
    FirstRunSeverity,
    first_run_issue_from_reason_code,
    first_run_report_payload,
)
from awf.host_setup.source_assets import SourceCheckoutError, VerifiedSourceCheckout
from awf.host_setup.system_checks.checks_core import (
    check_compose,
    check_disk,
    check_docker,
    check_gh,
    check_git,
    check_local_capacity,
    check_ports,
    check_postgres_port,
    check_python_runtime,
    check_shell_path,
)
from awf.host_setup.system_checks.checks_host import (
    _invalid_auth_mount_home_fallback,
    _invalid_host_home_override,
    _invalid_host_work_dir_override,
    _invalid_work_dir_home_fallback,
    _resolve_work_dir,
    check_auth_mount_home_fallback,
    check_host_home,
    check_host_home_override,
    check_host_work_dir_override,
    check_required_service_env,
    check_work_dir_home_fallback,
)
from awf.host_setup.system_checks.checks_ports import (
    _env_ollama_bridge_listen_port,
    _invalid_api_host_port_override,
    _invalid_ollama_bridge_bind_address,
    _invalid_ollama_bridge_listen_port_override,
    _invalid_postgres_host_port_override,
    _ollama_bridge_profile_enabled,
    _resolve_api_host_port,
    _resolve_postgres_host_port,
    check_api_host_port_override,
    check_host_port_conflict,
    check_ollama_bridge_api_port_conflict,
    check_ollama_bridge_bind_address,
    check_ollama_bridge_listen_port,
    check_ollama_bridge_postgres_port_conflict,
    check_ollama_bridge_target_host,
    check_ollama_bridge_target_port,
    check_postgres_host_port_override,
)
from awf.host_setup.system_checks.primitives import (
    DEFAULT_OLLAMA_BRIDGE_BIND_ADDRESS,
    DEFAULT_OLLAMA_BRIDGE_LISTEN_PORT,
    DEFAULT_OLLAMA_BRIDGE_TARGET_HOST,
    DEFAULT_OLLAMA_BRIDGE_TARGET_PORT,
    DEFAULT_POSTGRES_HOST_PORT,
    MIN_FREE_DISK_BYTES,
    MIN_MEMORY_BYTES,
    MIN_USABLE_CPUS,
    MINIMUM_PYTHON,
    SETUP_COMMAND,
    CommandResult,
    CommandRunner,
    CpuCountFn,
    FreeDiskFn,
    MemoryFn,
    PortProbeFn,
    PortProbeResult,
    SetupCheckError,
    SetupCheckLevel,
    SetupCheckResult,
    WhichFn,
    _docker_probe_runner,
    _docker_probe_which,
)


def run_system_checks(
    *,
    work_dir: Path | None = None,
    port: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[SetupCheckResult]:
    """Run the host system checks in a stable order and return the results.

    The Docker Compose probe is skipped when the Docker CLI is unavailable, so a
    missing Docker install surfaces as a single blocker (the docker check) rather
    than a duplicate compose failure for the same root cause.
    """
    invalid_api_host_port = _invalid_api_host_port_override(port=port, environ=environ)
    resolved_port: int | None = None
    if invalid_api_host_port is not None:
        ports_check = check_api_host_port_override(invalid_api_host_port)
    else:
        resolved_port = _resolve_api_host_port(port=port, environ=environ)
        ports_check = check_ports(resolved_port)
    invalid_postgres_host_port = _invalid_postgres_host_port_override(environ=environ)
    resolved_postgres_port: int | None = None
    if invalid_postgres_host_port is not None:
        postgres_port_check = check_postgres_host_port_override(invalid_postgres_host_port)
    else:
        resolved_postgres_port = _resolve_postgres_host_port(environ=environ)
        postgres_port_check = check_postgres_port(resolved_postgres_port)
    # Cross-check the two resolved host ports: each single-port probe binds and
    # releases independently, so a same-port collision passes both yet still
    # breaks awf start (Docker cannot reserve 0.0.0.0 and 127.0.0.1 on one port).
    # Skip when either override is invalid -- the override blocker already fires
    # and there is no resolved port to compare.
    port_conflict_check = (
        check_host_port_conflict(resolved_port, resolved_postgres_port)
        if resolved_port is not None and resolved_postgres_port is not None
        else None
    )
    # The optional ``ollama-bridge`` profile, when enabled in the resolved service
    # env, makes ``awf start`` publish a host-networking bridge bound to
    # ``${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}``
    # and forwarded to the socat TCP target
    # ``${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}``.
    # Validate the listen/target ports and bind/target hosts verbatim (each helper
    # returns ``None`` when the profile is off, so disabled-profile setups emit no
    # extra readiness line).
    ollama_bridge_port_check = check_ollama_bridge_listen_port(environ)
    ollama_bridge_bind_address_check = check_ollama_bridge_bind_address(environ)
    ollama_bridge_target_port_check = check_ollama_bridge_target_port(environ)
    ollama_bridge_target_host_check = check_ollama_bridge_target_host(environ)
    # Cross-check the API host port against the bridge listen port. The bridge
    # comes up before the API publish (bootstrap orders ollama_bridge ahead of
    # api_worker), and the API publishes on the wildcard 0.0.0.0, which overlaps
    # any specific bridge bind address on the same port, so a shared port breaks
    # awf start while both isolated probes still pass. Only applicable when the
    # bridge profile is on and both ports resolve -- skip when either override is
    # invalid (its own blocker already fires and there is no resolved port to
    # compare), mirroring the API/Postgres port_conflict gating above.
    bridge_env = os.environ if environ is None else environ
    bridge_profile_enabled = _ollama_bridge_profile_enabled(bridge_env)
    resolved_bridge_listen_port: int | None = None
    if bridge_profile_enabled and _invalid_ollama_bridge_listen_port_override(bridge_env) is None:
        resolved_bridge_listen_port = (
            _env_ollama_bridge_listen_port(bridge_env) or DEFAULT_OLLAMA_BRIDGE_LISTEN_PORT
        )
    ollama_bridge_port_conflict_check = (
        check_ollama_bridge_api_port_conflict(resolved_port, resolved_bridge_listen_port)
        if resolved_port is not None and resolved_bridge_listen_port is not None
        else None
    )
    # Cross-check the Postgres host port against the bridge listen port. Unlike the
    # API publish (a wildcard 0.0.0.0 that overlaps any address), both Postgres and
    # the bridge bind *specific* addresses, so a shared port only collides when the
    # resolved bridge bind address overlaps Postgres's 127.0.0.1 loopback. Resolve
    # the bind address only when the profile is on and its override is valid -- a
    # malformed bind address fires its own blocker and leaves nothing to compare --
    # then gate on a resolved Postgres port too (its own override blocker fires when
    # invalid), mirroring the API/bridge gating above.
    resolved_bridge_bind_address: str | None = None
    if bridge_profile_enabled and _invalid_ollama_bridge_bind_address(bridge_env) is None:
        resolved_bridge_bind_address = (
            bridge_env.get("AWF_OLLAMA_BRIDGE_BIND_ADDRESS") or DEFAULT_OLLAMA_BRIDGE_BIND_ADDRESS
        )
    ollama_bridge_postgres_port_conflict_check = (
        check_ollama_bridge_postgres_port_conflict(
            resolved_postgres_port,
            resolved_bridge_listen_port,
            resolved_bridge_bind_address,
        )
        if resolved_postgres_port is not None
        and resolved_bridge_listen_port is not None
        and resolved_bridge_bind_address is not None
        else None
    )
    invalid_work_dir = _invalid_host_work_dir_override(work_dir=work_dir, environ=environ)
    invalid_work_dir_home = _invalid_work_dir_home_fallback(work_dir=work_dir, environ=environ)
    if invalid_work_dir is not None:
        disk_check = check_host_work_dir_override(invalid_work_dir)
    elif invalid_work_dir_home is not None:
        # No usable AWF_HOST_WORK_DIR override, so Compose binds
        # ${HOME}/.awf/service verbatim. A relative, ~-prefixed, or
        # whitespace-padded HOME makes that bind path non-absolute (or spaced),
        # so awf start cannot mount it even though the probe would normalize it.
        disk_check = check_work_dir_home_fallback(invalid_work_dir_home)
    else:
        resolved_work_dir = _resolve_work_dir(work_dir=work_dir, environ=environ)
        disk_check = check_disk(resolved_work_dir)
    # ``AWF_HOST_HOME`` feeds the same verbatim-interpolation trap as the work
    # dir: the local-service Compose stack uses ${AWF_HOST_HOME:-${HOME}} as both
    # the host source and the absolute-required container target for every auth
    # mount, so a relative, ~-prefixed, or whitespace-padded value passes the
    # readiness probe yet makes ``awf start`` fail to mount the auth directories.
    # Block on it here rather than declaring the machine ready. When AWF_HOST_HOME
    # is unset the same trap applies to the ${HOME} fall-back the auth mounts use.
    invalid_host_home = _invalid_host_home_override(environ=environ)
    invalid_host_home_fallback = _invalid_auth_mount_home_fallback(environ=environ)
    if invalid_host_home is not None:
        host_home_check = check_host_home_override(invalid_host_home)
    elif invalid_host_home_fallback is not None:
        host_home_check = check_auth_mount_home_fallback(invalid_host_home_fallback)
    else:
        host_home_check = check_host_home(environ=environ)
    # Probe the daemon ``awf start`` will use: the resolved service env can point
    # Docker at a different host (``AWF_DOCKER_HOST``) or blank an inherited
    # ``DOCKER_HOST``, so feed that selection into both the docker and compose
    # probes instead of silently inheriting the bare process environment. The
    # runner locates ``docker`` via the resolved env's PATH (subprocess honours
    # ``env['PATH']`` for executable resolution), so the binary-presence gate must
    # search that same PATH -- otherwise a ``docker`` reachable only through the
    # service env's PATH would be reported "not installed" before the runner is
    # even tried.
    docker_runner = _docker_probe_runner(environ)
    docker_which = _docker_probe_which(environ)
    docker_check = check_docker(which=docker_which, run=docker_runner)
    # When the Docker CLI binary is absent, the ``docker compose`` plugin cannot
    # exist either: check_compose would re-probe the same missing binary and
    # append a second BLOCKED result for one root cause (a missing Docker
    # install). Guard the compose probe on check_docker's ``available`` flag so
    # that root cause surfaces exactly once. A reachable binary whose daemon is
    # down keeps ``available`` true, so the plugin is still probed --
    # ``docker compose version`` reports the plugin without contacting the daemon.
    compose_checks = (
        [check_compose(run=docker_runner)] if docker_check.data.get("available") else []
    )
    return [
        docker_check,
        *compose_checks,
        check_git(),
        check_gh(),
        check_python_runtime(),
        ports_check,
        postgres_port_check,
        *([port_conflict_check] if port_conflict_check is not None else []),
        *([ollama_bridge_port_check] if ollama_bridge_port_check is not None else []),
        *(
            [ollama_bridge_bind_address_check]
            if ollama_bridge_bind_address_check is not None
            else []
        ),
        *([ollama_bridge_target_port_check] if ollama_bridge_target_port_check is not None else []),
        *([ollama_bridge_target_host_check] if ollama_bridge_target_host_check is not None else []),
        *(
            [ollama_bridge_port_conflict_check]
            if ollama_bridge_port_conflict_check is not None
            else []
        ),
        *(
            [ollama_bridge_postgres_port_conflict_check]
            if ollama_bridge_postgres_port_conflict_check is not None
            else []
        ),
        disk_check,
        host_home_check,
        check_required_service_env(environ=environ),
        check_shell_path(),
        check_local_capacity(),
    ]


# --- Provider validation / interactive guard ------------------------------

KNOWN_SETUP_PROVIDERS: frozenset[str] = frozenset(
    {"github", "codex", "claude_code", "cursor", "gemini", "opencode", "grok", "awf_cloud"}
)
_PROVIDER_ALIASES: Mapping[str, str] = {
    "openai": "codex",
    "claude": "claude_code",
    "claudecode": "claude_code",
    "anthropic": "claude_code",
    "ollama": "opencode",
    "google": "gemini",
    # xAI is the brand behind Grok Build and the credential key (``xai``) the
    # adapters/recovery surfaces use, so accept it as the Grok selector alias.
    "xai": "grok",
    "awfcloud": "awf_cloud",
    "cloud": "awf_cloud",
}


def normalize_provider(name: str) -> str:
    """Normalize a provider selector to a known canonical name.

    Raises ``SetupCheckError(SETUP_PROVIDER_UNKNOWN)`` for unsupported names so
    setup never silently falls back to configuring all providers.
    """
    normalized = name.strip().lower().replace("-", "_")
    normalized = _PROVIDER_ALIASES.get(normalized, normalized)
    if normalized not in KNOWN_SETUP_PROVIDERS:
        raise SetupCheckError(
            f"Unsupported provider selector: {name!r}.",
            reason_code=SETUP_PROVIDER_UNKNOWN,
            details={"provider": name, "known_providers": sorted(KNOWN_SETUP_PROVIDERS)},
        )
    return normalized


def normalize_providers(names: Iterable[str]) -> list[str]:
    """Normalize and de-duplicate provider selectors while preserving order."""
    ordered: list[str] = []
    for name in names:
        normalized = normalize_provider(name)
        if normalized not in ordered:
            ordered.append(normalized)
    return ordered


def require_interactive(non_interactive: bool, what: str) -> None:
    """Raise ``INTERACTIVE_INPUT_REQUIRED`` when input is needed but unavailable."""
    if non_interactive:
        raise SetupCheckError(
            f"AWF setup needs interactive input to {what}.",
            reason_code=INTERACTIVE_INPUT_REQUIRED,
            details={"needs": what},
        )


# --- Readiness payload ----------------------------------------------------


def build_setup_readiness_payload(
    results: Sequence[SetupCheckResult],
    *,
    command: str = SETUP_COMMAND,
    selected_providers: Sequence[str] = (),
    allow_plain_secrets: bool = False,
    dry_run: bool = False,
    source_checkout: VerifiedSourceCheckout | None = None,
    source_checkout_error: SourceCheckoutError | None = None,
) -> FirstRunPayload:
    """Aggregate check results into a rendered first-run readiness payload."""
    issues: list[FirstRunIssue] = []
    if source_checkout_error is not None:
        issues.append(_source_checkout_issue(source_checkout_error))
    for result in results:
        issue = _readiness_issue(result)
        if issue is not None:
            issues.append(issue)

    blocked = [issue for issue in issues if issue.severity in ("blocked", "failed")]
    warnings = [issue for issue in issues if issue.severity == "warning"]

    details: dict[str, Any] = {
        "dry_run": dry_run,
        # Named without "secret" so the redaction layer does not mask this
        # non-secret boolean consent flag in rendered output.
        "plain_file_consent": allow_plain_secrets,
        "selected_providers": list(selected_providers),
        "checks": [{"name": result.name, "level": result.level.value} for result in results],
    }
    if source_checkout is not None:
        details["source_checkout"] = {
            "root": str(source_checkout.root),
            "verified_at": source_checkout.verified_at.isoformat(),
        }

    summary = _readiness_summary(blocked_count=len(blocked), warning_count=len(warnings))
    next_steps = _readiness_next_steps(blocked=bool(blocked))

    return first_run_report_payload(
        command=command,
        summary=summary,
        issues=issues,
        details=details,
        next_steps=next_steps,
    )


def _readiness_issue(result: SetupCheckResult) -> FirstRunIssue | None:
    """Map a non-OK check result to a reason-coded first-run issue."""
    if result.level is SetupCheckLevel.OK:
        return None
    severity: FirstRunSeverity = "blocked" if result.level is SetupCheckLevel.BLOCKED else "warning"
    details: dict[str, Any] = {"check": result.name, **dict(result.data)}
    return first_run_issue_from_reason_code(
        SETUP_READINESS_FAILED,
        severity=severity,
        details=details,
        problem=result.summary,
        cause=result.detail,
        fix=result.fix,
        docs_link=result.docs_link,
    )


def _source_checkout_issue(error: SourceCheckoutError) -> FirstRunIssue:
    """Map a source-checkout validation error to a blocked first-run issue."""
    details: dict[str, Any] = {"check": "source_checkout", "root": str(error.root)}
    if error.missing_markers:
        details["missing_markers"] = list(error.missing_markers)
    for key, value in error.details.items():
        details.setdefault(key, value)
    return first_run_issue_from_reason_code(
        error.reason_code,
        severity="blocked",
        details=details,
        problem=error.message,
    )


def _readiness_summary(*, blocked_count: int, warning_count: int) -> str:
    """Return a status summary line for the readiness payload."""
    if blocked_count:
        return (
            f"AWF setup found {blocked_count} readiness blocker(s) and {warning_count} warning(s)."
        )
    if warning_count:
        return f"AWF setup host readiness passed with {warning_count} warning(s)."
    return "AWF setup host readiness checks passed; this machine can run AWF Core."


def _readiness_next_steps(*, blocked: bool) -> tuple[str, ...]:
    """Return the next-command guidance for the readiness payload."""
    if blocked:
        return ("Fix the reported blockers above, then re-run awf setup --dry-run.",)
    return ("Run awf start to start local AWF Core.",)


__all__ = [
    "DEFAULT_OLLAMA_BRIDGE_BIND_ADDRESS",
    "DEFAULT_OLLAMA_BRIDGE_LISTEN_PORT",
    "DEFAULT_OLLAMA_BRIDGE_TARGET_HOST",
    "DEFAULT_OLLAMA_BRIDGE_TARGET_PORT",
    "DEFAULT_POSTGRES_HOST_PORT",
    "KNOWN_SETUP_PROVIDERS",
    "MINIMUM_PYTHON",
    "MIN_FREE_DISK_BYTES",
    "MIN_MEMORY_BYTES",
    "MIN_USABLE_CPUS",
    "SETUP_COMMAND",
    "CommandResult",
    "CommandRunner",
    "CpuCountFn",
    "FreeDiskFn",
    "MemoryFn",
    "PortProbeFn",
    "PortProbeResult",
    "SetupCheckError",
    "SetupCheckLevel",
    "SetupCheckResult",
    "WhichFn",
    "build_setup_readiness_payload",
    "check_api_host_port_override",
    "check_auth_mount_home_fallback",
    "check_compose",
    "check_disk",
    "check_docker",
    "check_gh",
    "check_git",
    "check_host_home",
    "check_host_home_override",
    "check_host_port_conflict",
    "check_host_work_dir_override",
    "check_local_capacity",
    "check_ollama_bridge_api_port_conflict",
    "check_ollama_bridge_bind_address",
    "check_ollama_bridge_listen_port",
    "check_ollama_bridge_postgres_port_conflict",
    "check_ollama_bridge_target_host",
    "check_ollama_bridge_target_port",
    "check_ports",
    "check_postgres_host_port_override",
    "check_postgres_port",
    "check_python_runtime",
    "check_required_service_env",
    "check_shell_path",
    "check_work_dir_home_fallback",
    "normalize_provider",
    "normalize_providers",
    "require_interactive",
    "run_system_checks",
]
