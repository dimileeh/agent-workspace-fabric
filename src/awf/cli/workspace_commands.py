"""Workspace, lock, and operation CLI command groups."""

from __future__ import annotations

import urllib.parse
from typing import Any

import typer

from awf.cli.common import (
    MinRichHelpWidthCommand,
    OutputFormat,
    _api_token_headers,
    _api_token_option,
    _base_url,
    _call,
    _control_headers,
    _control_idempotency_key_option,
    _handle_response,
    _parse_json_option,
)
from awf.cli.companion_env import merge_companion_env
from awf.cli.env_file import parse_env_exclude_arg, parse_env_from_arg
from awf.db.enums import (
    AgentRuntime,
    OperationStatus,
    OperationType,
    TaskClass,
    TaskKind,
    WorkspaceStatus,
)

_DX_FIRST_PATH_HELP = (
    "For first-time users: the current runnable first path is "
    "`awf service bootstrap` (or the friendly `awf start` wrapper), then "
    "`awf init <path>` to prepare your project repository. Run `awf setup "
    "--dry-run` first for a read-only host readiness check."
)

workspace_app = typer.Typer(
    help=f"Workspace lifecycle (create/inspect/destroy).\n{_DX_FIRST_PATH_HELP}"
)
locks_app = typer.Typer(help="Owned-path reservation and overlap-risk visibility.")
operations_app = typer.Typer(help="Global operation history inspection.")


def _option_value(value: Any) -> str:
    """Return the wire value for Typer enum options and direct string test calls."""
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


@workspace_app.command("create")
def workspace_create(
    repo_url: str = typer.Option(..., "--repo", help="Git URL."),
    task_title: str = typer.Option(..., "--title"),
    task_prompt: str = typer.Option(..., "--prompt"),
    branch_base: str | None = typer.Option(
        None,
        "--base",
        help=(
            "Base/target branch. Defaults to 'development' for feature_branch_pr "
            "and 'main' for sync_release_pr."
        ),
    ),
    task_kind: TaskKind = typer.Option(
        TaskKind.feature_branch_pr,
        "--task-kind",
        help="feature_branch_pr (default) or sync_release_pr.",
    ),
    source_branch: str | None = typer.Option(
        None,
        "--source-branch",
        help="Source branch for sync_release_pr release PRs (default development).",
    ),
    agent: str = typer.Option("codex", "--agent"),
    model: str | None = typer.Option(None, "--model"),
    effort: str | None = typer.Option(
        None,
        "--effort",
        help="Optional reasoning effort override for the selected agent runtime.",
    ),
    task_class: TaskClass | None = typer.Option(None, "--task-class"),
    priority: int | None = typer.Option(None, "--priority"),
    human_boost: int | None = typer.Option(None, "--human-boost"),
    out_of_scope_changes_json: str | None = typer.Option(
        None,
        "--out-of-scope-changes-json",
        "--out_of_scope_changes_json",
        help="JSON payload for task out_of_scope_changes policy.",
    ),
    provider_recovery_json: str | None = typer.Option(
        None,
        "--provider-recovery-json",
        "--provider_recovery_json",
        help="JSON payload for task provider-recovery policy.",
    ),
    owned_paths: list[str] | None = typer.Option(None, "--owned-path", help="Repeatable."),
    external_id: str | None = typer.Option(None, "--external-id"),
    cpu: float | None = typer.Option(None, "--cpu"),
    memory: str | None = typer.Option(None, "--memory"),
    steady_state_cpu_cores: float | None = typer.Option(None, "--steady-state-cpu-cores"),
    steady_state_memory_gb: float | None = typer.Option(None, "--steady-state-memory-gb"),
    peak_cpu_cores: float | None = typer.Option(None, "--peak-cpu-cores"),
    peak_memory_gb: float | None = typer.Option(None, "--peak-memory-gb"),
    disk_mb: int | None = typer.Option(None, "--disk-mb"),
    profile_ref: str = typer.Option("auto", "--profile"),
    test_commands: list[str] = typer.Option([], "--test", help="Repeatable."),
    requires_database: bool = typer.Option(
        False,
        "--with-db",
        help="Deprecated v1 shortcut; selects the aira profile when set.",
    ),
    auto_merge: bool = typer.Option(
        True,
        "--auto-merge/--no-auto-merge",
        help="Allow the monitor to merge when PR gates are green.",
    ),
    initial_review_grace_period_seconds: float | None = typer.Option(
        None,
        "--initial-review-grace-period-seconds",
        min=0,
        max=86400,
        help="Override profile monitor grace; omit to use the profile setting.",
    ),
    provider_readiness_override: bool = typer.Option(
        False,
        "--provider-readiness-override",
        help="Explicitly admit launch when selected provider readiness is not ready.",
    ),
    provider_readiness_override_reason: str | None = typer.Option(
        None,
        "--provider-readiness-override-reason",
        help="Audit reason for --provider-readiness-override.",
    ),
    companion_json: list[str] | None = typer.Option(
        None,
        "--companion-json",
        help="Repeatable JSON companion service definition.",
    ),
    companion_env_from: list[str] | None = typer.Option(
        None,
        "--companion-env-from",
        help=(
            "Repeatable. Read a .env file and merge its vars into the "
            "matching companion's environment. Format: name=path. Explicit "
            "--companion-json values win over file values."
        ),
    ),
    companion_env_exclude: list[str] | None = typer.Option(
        None,
        "--companion-env-exclude",
        help=(
            "Repeatable. Drop named keys from a companion's merged environment. "
            "Format: name=KEY1,KEY2,..."
        ),
    ),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Submit a workspace creation request."""
    task_kind_value = _option_value(task_kind)
    if branch_base is None:
        branch_base = "main" if task_kind_value == TaskKind.sync_release_pr.value else "development"
    repo_body: dict[str, Any] = {"url": repo_url, "base_branch": branch_base}
    if source_branch is not None:
        repo_body["source_branch"] = source_branch
    body: dict[str, Any] = {
        "repo": repo_body,
        "task": {
            "title": task_title,
            "prompt": task_prompt,
            "agent": agent,
            "kind": task_kind_value,
            "auto_merge": auto_merge,
            "initial_review_grace_period_seconds": initial_review_grace_period_seconds,
        },
        "workspace": {"profile_ref": "aira" if requires_database else profile_ref, "profile": None},
        "validation": {"commands": test_commands, "requested_tier": 1},
        "resources": {},
        "preflight": {
            "provider_readiness_override": provider_readiness_override,
            "provider_readiness_override_reason": provider_readiness_override_reason,
        },
        "companions": [],
    }

    if model is not None:
        body["task"]["model"] = model
    if effort is not None:
        body["task"]["effort"] = effort
    if task_class is not None:
        body["task"]["task_class"] = _option_value(task_class)
    if external_id is not None:
        body["task"]["external_id"] = external_id
    if priority is not None:
        body["task"]["priority"] = priority
    if human_boost is not None:
        body["task"]["human_boost"] = human_boost
    if out_of_scope_changes_json is not None:
        body["task"]["out_of_scope_changes"] = _parse_json_option(
            "--out-of-scope-changes-json",
            out_of_scope_changes_json,
        )
    if provider_recovery_json is not None:
        body["task"]["provider_recovery"] = _parse_json_option(
            "--provider-recovery-json",
            provider_recovery_json,
        )
    if owned_paths is not None:
        body["task"]["owned_paths"] = owned_paths

    if cpu is not None:
        body["resources"]["cpu"] = cpu
    if memory is not None:
        body["resources"]["memory"] = memory
    if steady_state_cpu_cores is not None:
        body["resources"]["steady_state_cpu_cores"] = steady_state_cpu_cores
    if steady_state_memory_gb is not None:
        body["resources"]["steady_state_memory_gb"] = steady_state_memory_gb
    if peak_cpu_cores is not None:
        body["resources"]["peak_cpu_cores"] = peak_cpu_cores
    if peak_memory_gb is not None:
        body["resources"]["peak_memory_gb"] = peak_memory_gb
    if disk_mb is not None:
        body["resources"]["disk_mb"] = disk_mb
    if companion_json is not None:
        body["companions"] = [
            _parse_json_option("--companion-json", companion) for companion in companion_json
        ]

    if companion_env_from is not None or companion_env_exclude is not None:
        try:
            env_from_pairs: list[tuple[str, str]] = []
            if companion_env_from:
                for arg in companion_env_from:
                    env_from_pairs.append(parse_env_from_arg(arg))
            env_exclude_parsed: list[tuple[str, set[str]]] = []
            if companion_env_exclude:
                for arg in companion_env_exclude:
                    env_exclude_parsed.append(parse_env_exclude_arg(arg))
            body["companions"] = merge_companion_env(
                body.get("companions", []),
                env_from=env_from_pairs,
                env_exclude=env_exclude_parsed,
            )
        except (ValueError, FileNotFoundError, PermissionError, UnicodeDecodeError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from None

    headers = _api_token_headers(api_token)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    response = _call(
        "POST",
        "/v1/workspaces",
        base_url=_base_url(base_url),
        json=body,
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("show")
def workspace_show(
    workspace_id: str = typer.Argument(...),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Fetch the current state of one workspace."""
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("retry")
def workspace_retry(
    workspace_id: str = typer.Argument(...),
    provider_readiness_override: bool = typer.Option(
        False,
        "--provider-readiness-override",
        help="Explicitly admit retry when selected provider readiness is not ready.",
    ),
    provider_readiness_override_reason: str | None = typer.Option(
        None,
        "--provider-readiness-override-reason",
        help="Audit reason for --provider-readiness-override.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Retry a failed or cancelled workspace as a fresh attempt."""
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/retry",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
        params=(
            {
                "provider_readiness_override": provider_readiness_override,
                "provider_readiness_override_reason": provider_readiness_override_reason,
            }
            if provider_readiness_override or provider_readiness_override_reason is not None
            else None
        ),
    )
    _handle_response(response, fmt)


@workspace_app.command("remonitor")
def workspace_remonitor(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request PR monitor recovery for a monitoring workspace."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="remonitor",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/remonitor",
        base_url=_base_url(base_url),
        json={"reason": reason},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("cancel")
def workspace_cancel(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    stop_stack: bool = typer.Option(
        True,
        "--stop-stack/--no-stop-stack",
        help="Whether to stop workspace runtime resources before cancellation.",
    ),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request cancellation for one workspace."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="cancel",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/cancel",
        base_url=_base_url(base_url),
        json={"reason": reason, "stop_stack": stop_stack},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("stop")
def workspace_stop(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request stack stop for one workspace."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="stop",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/stop",
        base_url=_base_url(base_url),
        json={"reason": reason},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("destroy")
def workspace_destroy(
    workspace_id: str = typer.Argument(...),
    force: bool = typer.Option(
        False,
        "--force",
        help="Force destroy even when workspace state is active.",
    ),
    remove_volumes: bool = typer.Option(
        True,
        "--remove-volumes/--no-remove-volumes",
        help="Whether to remove workspace volumes.",
    ),
    remove_worktree: bool = typer.Option(
        True,
        "--remove-worktree/--no-remove-worktree",
        help="Whether to remove workspace worktree.",
    ),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request destruction of a workspace and optional related resources."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="destroy",
    )
    response = _call(
        "DELETE",
        f"/v1/workspaces/{workspace_id}",
        base_url=_base_url(base_url),
        params={
            "force": force,
            "remove_volumes": remove_volumes,
            "remove_worktree": remove_worktree,
        },
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("refresh")
def workspace_refresh(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Trigger drift refresh for one workspace."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="refresh",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/refresh",
        base_url=_base_url(base_url),
        json={"reason": reason},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("validate")
def workspace_validate(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    requested_tier: int | None = typer.Option(
        None,
        "--requested-tier",
        min=1,
        max=3,
        help="Optional validation tier (1-3).",
    ),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request revalidation for one workspace."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="validate",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/validate",
        base_url=_base_url(base_url),
        json={"reason": reason, "requested_tier": requested_tier},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("rebase")
def workspace_rebase(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request workspace rebase onto the current target branch."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="rebase",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/rebase",
        base_url=_base_url(base_url),
        json={"reason": reason},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("adopt-pr", cls=MinRichHelpWidthCommand)
def workspace_adopt_pr(
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="GitHub repo slug or URL. Use with --pr.",
    ),
    pr_number: int | None = typer.Option(
        None,
        "--pr",
        min=1,
        help="Pull request number. Use with --repo.",
    ),
    pr_url: str | None = typer.Option(
        None,
        "--pr-url",
        help="Full GitHub pull request URL.",
    ),
    agent: str = typer.Option("codex", "--agent"),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Optional model override for the adopted PR monitor's selected agent.",
    ),
    effort: str | None = typer.Option(
        None,
        "--effort",
        help="Optional reasoning effort override for the adopted PR monitor.",
    ),
    owned_paths: list[str] | None = typer.Option(
        None,
        "--owned-path",
        help=(
            "Repeatable operator-approved owned path for the adopted monitor. "
            "Use this when PR review/CI repair is expected to touch protected files."
        ),
    ),
    profile_ref: str | None = typer.Option("auto", "--profile"),
    auto_merge: bool = typer.Option(
        True,
        "--auto-merge/--no-auto-merge",
        help="Allow the adopted PR monitor to merge when gates are green.",
    ),
    initial_review_grace_period_seconds: float | None = typer.Option(
        None,
        "--initial-review-grace-period-seconds",
        min=0,
        max=86400,
        help="Override profile monitor grace; omit to use the profile setting.",
    ),
    task_title: str | None = typer.Option(None, "--title"),
    task_prompt: str | None = typer.Option(None, "--prompt"),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Adopt an already-open GitHub PR into AWF PR monitoring."""
    if pr_url is None and (repo is None or pr_number is None):
        raise typer.BadParameter(
            "select a PR with exactly one selector: either --pr-url or both --repo and --pr"
        )
    if pr_url is not None and (repo is not None or pr_number is not None):
        raise typer.BadParameter(
            "select a PR with exactly one selector: either --pr-url or both --repo and --pr"
        )
    body: dict[str, Any] = {
        "repo_url": repo if repo and "github.com" in repo else None,
        "repo_slug": repo if repo and "github.com" not in repo else None,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "agent": agent,
        "profile_ref": profile_ref,
        "profile": None,
        "auto_merge": auto_merge,
        "initial_review_grace_period_seconds": initial_review_grace_period_seconds,
        "task_title": task_title,
        "task_prompt": task_prompt,
        "reason": reason,
    }
    if model is not None:
        body["model"] = model
    if effort is not None:
        body["effort"] = effort
    if owned_paths is not None:
        body["owned_paths"] = owned_paths
    response = _call(
        "POST",
        "/v1/workspaces/adopt-pr",
        base_url=_base_url(base_url),
        json=body,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("list")
def workspace_list(
    status: list[WorkspaceStatus] | None = typer.Option(None, "--status"),
    agent: AgentRuntime | None = typer.Option(None, "--agent"),
    repo_url: str | None = typer.Option(None, "--repo-url"),
    limit: int = typer.Option(50, "--limit"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List workspaces (newest first)."""
    params_list: list[tuple[str, Any]] = [("limit", limit)]
    if status:
        for s in status:
            params_list.append(("status", s.value))
    if agent is not None:
        params_list.append(("agent", agent.value))
    if repo_url is not None:
        params_list.append(("repo_url", repo_url))

    response = _call(
        "GET",
        "/v1/workspaces",
        base_url=_base_url(base_url),
        params=params_list,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@locks_app.command("list")
def locks_list(
    repo_url: str | None = typer.Option(None, "--repo-url"),
    task_class: TaskClass | None = typer.Option(None, "--task-class"),
    status: WorkspaceStatus | None = typer.Option(None, "--status"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List workspace owned-path reservations and overlap risks."""
    params: dict[str, Any] = {"limit": limit}
    if repo_url is not None:
        params["repo_url"] = repo_url
    if task_class is not None:
        params["task_class"] = task_class.value
    if status is not None:
        params["status"] = status.value
    response = _call(
        "GET",
        "/v1/locks",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt, pretty_items=True)


@workspace_app.command("events")
def workspace_events(
    workspace_id: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    event_type: str | None = typer.Option(None, "--event-type"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List immutable events for one workspace."""
    params: dict[str, Any] = {"limit": limit}
    if event_type is not None:
        params["event_type"] = event_type
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/events",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("runtime")
def workspace_runtime(
    workspace_id: str = typer.Argument(...),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Fetch runtime/container state for one workspace."""
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/runtime",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("operations")
def workspace_operations(
    workspace_id: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    cursor: str | None = typer.Option(None, "--cursor", "--after"),
    status: OperationStatus | None = typer.Option(None, "--status"),
    operation_type: OperationType | None = typer.Option(None, "--type"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List operations for one workspace."""
    params: dict[str, Any] = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    if status is not None:
        params["status"] = status.value
    if operation_type is not None:
        params["type"] = operation_type.value
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/operations",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@operations_app.command("list")
def operations_list(
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    cursor: str | None = typer.Option(None, "--cursor", "--after"),
    status: OperationStatus | None = typer.Option(None, "--status"),
    operation_type: OperationType | None = typer.Option(None, "--type"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List global operations, optionally filtered by workspace."""
    params: dict[str, Any] = {"limit": limit}
    if workspace_id is not None:
        params["workspace_id"] = workspace_id
    if cursor is not None:
        params["cursor"] = cursor
    if status is not None:
        params["status"] = status.value
    if operation_type is not None:
        params["type"] = operation_type.value
    response = _call(
        "GET",
        "/v1/operations",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@operations_app.command("show")
def operations_show(
    operation_id: str = typer.Argument(...),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Fetch one operation by id."""
    operation_ref = urllib.parse.quote(operation_id, safe="")
    response = _call(
        "GET",
        f"/v1/operations/{operation_ref}",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("logs")
def workspace_logs(
    workspace_id: str = typer.Argument(...),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List durable log streams for one workspace."""
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/logs",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("log")
def workspace_log(
    workspace_id: str = typer.Argument(...),
    stream_id: str = typer.Argument(...),
    offset: int = typer.Option(0, "--offset", min=0),
    limit_bytes: int = typer.Option(65_536, "--limit-bytes", min=1, max=1_048_576),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Read a bounded durable log chunk for one stream."""
    encoded_stream_id = urllib.parse.quote(stream_id, safe="")
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/logs/{encoded_stream_id}",
        base_url=_base_url(base_url),
        params={"offset": offset, "limit_bytes": limit_bytes},
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)
