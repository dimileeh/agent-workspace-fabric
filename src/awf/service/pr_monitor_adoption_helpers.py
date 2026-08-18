"""Helpers and shared types for PR monitor adoption."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from typing import Any

from sqlalchemy import inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.adapters.defaults import defaults_with_model_overrides
from awf.api.schemas import (
    PullRequestMonitorAdoptionRequest,
)
from awf.common.audit import redact_audit_text
from awf.common.auto_merge import (
    AUTO_MERGE_INTENT_POLICY_KEY,
    auto_merge_intent_from_policy,
)
from awf.common.commands import AsyncioSubprocessRunner
from awf.common.config import Settings
from awf.common.forge import ForgeNotSupportedError, ensure_forge_supported
from awf.common.github_client import (
    PullRequestAdoptionMetadata,
    RepoRef,
    fetch_pull_request_adoption_metadata,
    parse_github_pull_request_url,
)
from awf.common.logging import get_logger
from awf.common.workspace_policy import pr_adoption_execution_policy
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Task, TaskAttempt, Workspace
from awf.db.repositories import (
    TaskExternalIdConflictError,
    WorkspaceRepository,
)
from awf.db.repositories.base import resolve_session_dialect_name
from awf.db.utils import escape_like_pattern as _escape_like_pattern
from awf.profiles.models import normalize_inline_profile_snapshot
from awf.runtime.hosted_delegation import (
    HostedDelegationConfigError,
)
from awf.service.config import (
    hosted_delegation_config_from_service_settings,
    resolve_service_settings,
)

PR_ADOPTION_REQUESTED_EVENT_TYPE = "workspace.pr_monitor_adoption_requested"
PR_ADOPTION_SUPERSEDED_EVENT_TYPE = "workspace.pr_monitor_adoption_superseded"
PR_ADOPTION_REQUESTED_REASON = "PR_MONITOR_ADOPTION_REQUESTED"
PR_ADOPTION_SUPERSEDED_REASON = "PR_ADOPTION_SUPERSEDED_TERMINAL_WORKSPACE"
PR_ADOPTION_ADMITTED_REASON = "PR_ADOPTION_ADMITTED"
PR_ADOPTION_OPERATION_ACTION = "adopt_pr_monitor"
PR_ADOPTION_TASK_KIND = "sync_feature_pr"
# Distinct from ``FORGE_NOT_SUPPORTED``: the forge itself *is* supported (issue
# #345 flipped bitbucket into ``_SUPPORTED_FORGES``), so a ``bitbucket.org`` ref
# clears the adoption forge gate. But the *default* adoption metadata fetcher
# shells ``gh pr view``, which is GitHub-only — so this honest code says "the
# default fetcher cannot serve this supported forge yet", not "unsupported forge".
PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY = "PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY"
_LIVE_ADOPTION_STATUSES = frozenset(
    {
        WorkspaceStatus.requested.value,
        WorkspaceStatus.provisioning.value,
        WorkspaceStatus.ready.value,
        WorkspaceStatus.running.value,
        WorkspaceStatus.validating.value,
        WorkspaceStatus.pushing.value,
        WorkspaceStatus.monitoring_pr.value,
    }
)
# Keep the public adoption error-code contract present in service source so
# docs parity tests can cross-reference the matrix against implementation.
_PR_ADOPTION_ERROR_CODE_CONTRACT = (
    {"error_code": "PR_ADOPTION_INPUT_REQUIRED"},
    {"error_code": "INVALID_GITHUB_REPO"},
    {"error_code": "FORGE_NOT_SUPPORTED"},
    {"error_code": "PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY"},
    {"error_code": "PR_NOT_FOUND"},
    {"error_code": "PR_ALREADY_CLOSED"},
    {"error_code": "PR_ALREADY_MERGED"},
    {"error_code": "PR_METADATA_FETCH_FAILED"},
    {"error_code": "PR_METADATA_INVALID"},
    {"error_code": "PR_ADOPTION_POLICY_CONFLICT"},
    {"error_code": "TASK_EXTERNAL_ID_CONFLICT"},
    {"error_code": "HOSTED_DELEGATION_NOT_CONFIGURED"},
)
_NON_RESUMABLE_ADOPTION_STATUSES = frozenset(
    {
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroying,
        WorkspaceStatus.destroyed,
    }
)
_log = get_logger(__name__)

MetadataFetcher = Callable[
    [RepoRef, int],
    Awaitable[PullRequestAdoptionMetadata],
]


class PRMonitorAdoptionError(Exception):
    """Structured adoption failure that REST/MCP can expose directly."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        status_code: int = 409,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)


async def _default_metadata_fetcher(
    *,
    repo: RepoRef,
    pr_number: int,
) -> PullRequestAdoptionMetadata:
    # ``fetch_pull_request_adoption_metadata`` shells ``gh pr view --repo
    # owner/repo``, which always targets github.com. Forge detection (issue #345)
    # makes ``RepoRef.from_url`` accept non-GitHub hosts, and the Part 2 gate flip
    # lets a ``bitbucket.org`` ref clear the adoption forge gate — so without this
    # guard the default fetcher would silently query GitHub for the *same* slug
    # (a different repo). Fail closed: a Bitbucket-aware adoption metadata fetcher
    # is follow-up work and must be injected explicitly, never reached by mis-route.
    if repo.forge != "github":
        raise PRMonitorAdoptionError(
            error_code=PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY,
            message=(
                "Default PR adoption metadata fetch is GitHub-only "
                "(uses `gh pr view`); forge "
                f"{repo.forge!r} requires an injected forge-aware metadata fetcher."
            ),
            status_code=422,
            detail={"repo_slug": repo.slug(), "forge": repo.forge},
        )
    return await fetch_pull_request_adoption_metadata(
        runner=AsyncioSubprocessRunner(),
        repo=repo,
        pr_number=pr_number,
    )


def _select_live_adoption_workspace(workspaces: list[Workspace]) -> Workspace | None:
    live_workspaces = [
        workspace for workspace in workspaces if _is_live_adoption_status(workspace.status)
    ]
    if not live_workspaces:
        return None
    return max(live_workspaces, key=lambda workspace: (workspace.created_at, workspace.id))


def _is_live_adoption_status(status: str) -> bool:
    return status in _LIVE_ADOPTION_STATUSES


async def _next_adoption_workspace_idempotency_key(
    workspace_repo: WorkspaceRepository,
    *,
    logical_idempotency_key: str,
    known_workspace_keys: Iterable[str] | None = None,
    reserved_idempotency_keys: Iterable[str] = (),
    require_generation: bool = False,
) -> str:
    workspace_keys = set(await workspace_repo.list_idempotency_key_family(logical_idempotency_key))
    if known_workspace_keys is not None:
        workspace_keys.update(known_workspace_keys)
    if not require_generation and not workspace_keys:
        return logical_idempotency_key

    existing_keys = set(workspace_keys)
    existing_keys.update(reserved_idempotency_keys)

    for generation in range(1, 1000):
        candidate = f"{logical_idempotency_key}:g{generation}"
        if candidate not in existing_keys:
            return candidate
    raise RuntimeError("Could not allocate a fresh PR adoption workspace idempotency key.")


async def _task_idempotency_key_family(
    session: AsyncSession,
    *,
    logical_idempotency_key: str,
) -> list[str]:
    generation_pattern = f"{_escape_like_pattern(logical_idempotency_key)}:g%"
    stmt = (
        select(Task.idempotency_key)
        .where(
            or_(
                Task.idempotency_key == logical_idempotency_key,
                Task.idempotency_key.like(generation_pattern, escape="\\"),
            )
        )
        .order_by(Task.idempotency_key.asc())
    )
    keys = (await session.execute(stmt)).scalars().all()
    return [key for key in keys if key is not None]


async def _task_external_id_family_idempotency_keys(
    session: AsyncSession,
    *,
    logical_idempotency_key: str,
    task_external_id: str,
) -> list[str]:
    generation_pattern = f"{_escape_like_pattern(task_external_id)}:g%"
    stmt = (
        select(Task.external_id)
        .where(
            or_(
                Task.external_id == task_external_id,
                Task.external_id.like(generation_pattern, escape="\\"),
            )
        )
        .order_by(Task.external_id.asc())
    )
    external_ids = (await session.execute(stmt)).scalars().all()
    reserved_keys: list[str] = []
    generation_prefix = f"{task_external_id}:"
    for external_id in external_ids:
        if external_id == task_external_id:
            reserved_keys.append(logical_idempotency_key)
            continue
        if external_id is None or not external_id.startswith(generation_prefix):
            continue
        generation = external_id[len(generation_prefix) :]
        if _is_adoption_generation_suffix(generation):
            reserved_keys.append(f"{logical_idempotency_key}:{generation}")
    return list(dict.fromkeys(reserved_keys))


async def _next_adoption_task_idempotency_key(
    session: AsyncSession,
    *,
    logical_idempotency_key: str,
    task_external_id: str,
) -> str:
    existing_keys = set(
        await _task_idempotency_key_family(
            session,
            logical_idempotency_key=logical_idempotency_key,
        )
    )
    existing_keys.update(
        await _task_external_id_family_idempotency_keys(
            session,
            logical_idempotency_key=logical_idempotency_key,
            task_external_id=task_external_id,
        )
    )
    for generation in range(1, 1000):
        candidate = f"{logical_idempotency_key}:g{generation}"
        if candidate not in existing_keys:
            return candidate
    raise RuntimeError("Could not allocate a fresh PR adoption task idempotency key.")


async def _task_has_existing_attempt(session: AsyncSession, task_id: str) -> bool:
    stmt = select(TaskAttempt.id).where(TaskAttempt.task_id == task_id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _task_has_shared_ownership_attempt(
    session: AsyncSession,
    task_id: str,
    *,
    terminal_workspace_ids: Collection[str],
) -> bool:
    """True when *task_id* has an attempt outside the terminal adoption lineage.

    Peer PR adoptions that share an explicit external ID, and joined same-scope
    source workspaces, both establish shared ownership. Terminal re-adoption
    must rejoin that task rather than treat the logical key as an owned
    generation slot.
    """
    if not terminal_workspace_ids:
        return False
    stmt = (
        select(TaskAttempt.id)
        .where(
            TaskAttempt.task_id == task_id,
            TaskAttempt.workspace_id.notin_(tuple(terminal_workspace_ids)),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _adoption_owns_task_identity(
    session: AsyncSession,
    task: Task,
    *,
    adoption_idempotency_key: str,
    workspace_id: str,
) -> bool:
    """Return whether supersession may rewrite this task's identity slots.

    Key equality alone is insufficient: joining a same-scope source task whose
    ``idempotency_key`` was null causes ``TaskRepository._reuse_or_conflict`` to
    stamp the adoption key onto that shared row. Ownership therefore requires
    both a matching key and no attempt from any other workspace.

    Lock the task row before probing attempts so a concurrent join that already
    selected this task (but has not committed its ``TaskAttempt``) serializes
    with supersession instead of racing an unlocked existence query.
    """
    if task.idempotency_key != adoption_idempotency_key:
        return False
    if resolve_session_dialect_name(session, None) == "postgresql":
        await session.execute(select(Task.id).where(Task.id == task.id).with_for_update())
        locked = await session.get(Task, task.id)
        if locked is None or locked.idempotency_key != adoption_idempotency_key:
            return False
    stmt = (
        select(TaskAttempt.id)
        .where(
            TaskAttempt.task_id == task.id,
            TaskAttempt.workspace_id != workspace_id,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is None


def _is_adoption_generation_suffix(value: str) -> bool:
    return value.startswith("g") and value[1:].isdigit()


async def _terminal_adoption_lineage(
    session: AsyncSession,
    workspaces: list[Workspace],
) -> list[dict[str, str | None]]:
    terminal_workspaces = [
        workspace for workspace in workspaces if not _is_live_adoption_status(workspace.status)
    ]
    attempts_by_workspace_id: dict[str, TaskAttempt | None] = {}
    unloaded_workspace_ids: list[str] = []
    for workspace in terminal_workspaces:
        if "task_attempt" in inspect(workspace).unloaded:
            unloaded_workspace_ids.append(workspace.id)
        else:
            attempts_by_workspace_id[workspace.id] = workspace.task_attempt
    if unloaded_workspace_ids:
        stmt = select(TaskAttempt).where(TaskAttempt.workspace_id.in_(unloaded_workspace_ids))
        attempts = (await session.execute(stmt)).scalars()
        attempts_by_workspace_id.update({attempt.workspace_id: attempt for attempt in attempts})

    lineage: list[dict[str, str | None]] = []
    for workspace in terminal_workspaces:
        attempt = attempts_by_workspace_id.get(workspace.id)
        lineage.append(
            {
                "workspace_id": workspace.id,
                "status": workspace.status,
                "task_id": attempt.task_id if attempt is not None else None,
                "attempt_id": attempt.id if attempt is not None else None,
            }
        )
    return lineage


def _adoption_lineage_payload(
    *,
    logical_idempotency_key: str,
    previous_terminal_adoptions: list[dict[str, str | None]],
) -> dict[str, object] | None:
    if not previous_terminal_adoptions:
        return None
    return {
        "logical_idempotency_key": logical_idempotency_key,
        "previous_terminal_adoptions": previous_terminal_adoptions,
    }


def pr_adoption_idempotency_key(*, repo_slug: str, pr_number: int) -> str:
    digest = hashlib.sha256(f"{repo_slug.lower()}#{pr_number}".encode()).hexdigest()
    return f"pr-adopt:{digest[:48]}"


def _normalize_request_identity(
    request: PullRequestMonitorAdoptionRequest,
) -> tuple[RepoRef, int]:
    if request.pr_url:
        try:
            repo, pr_number = parse_github_pull_request_url(request.pr_url)
        except ValueError as exc:
            # ``parse_github_pull_request_url`` rejects ANY non-github.com host with
            # a bare ValueError, so a well-formed Bitbucket PR URL would surface the
            # generic input error instead of the contract-documented
            # FORGE_NOT_SUPPORTED. Re-parse the URL as a forge-aware ``RepoRef`` and
            # route a recognized-but-unsupported forge through the same gate the
            # ``repo_url``/``repo_slug`` path uses; a truly unparseable URL falls
            # through to PR_ADOPTION_INPUT_REQUIRED below.
            _raise_if_pr_url_forge_unsupported(request.pr_url)
            raise PRMonitorAdoptionError(
                error_code="PR_ADOPTION_INPUT_REQUIRED",
                message="Provide a valid GitHub PR URL or repo plus PR number.",
                status_code=422,
            ) from exc
        if request.pr_number is not None and request.pr_number != pr_number:
            raise PRMonitorAdoptionError(
                error_code="PR_ADOPTION_INPUT_REQUIRED",
                message="PR URL and pr_number refer to different pull requests.",
                status_code=422,
            )
        # Precedence: identity conflict > forge gate. A GitHub ``pr_url`` paired
        # with a Bitbucket ``repo_url`` of the same slug surfaces
        # PR_ADOPTION_INPUT_REQUIRED (the conflict fires first), not
        # FORGE_NOT_SUPPORTED — see
        # ``test_github_pr_url_with_same_slug_bitbucket_repo_url_rejected``. The
        # forge gate below is then defense-in-depth: a canonical ref parsed from a
        # ``github.com`` PR URL is always ``"github"``, so it never raises on this
        # path today.
        _raise_if_repo_identity_conflicts(canonical_repo=repo, request=request)
        _raise_if_forge_unsupported(repo)
        return repo, pr_number

    repo_value = request.repo_slug or request.repo_url
    if not repo_value or request.pr_number is None:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_INPUT_REQUIRED",
            message="Provide pr_url, or provide repo_url/repo_slug plus pr_number.",
            status_code=422,
        )
    try:
        repo = RepoRef.from_url(repo_value)
    except ValueError as exc:
        raise PRMonitorAdoptionError(
            error_code="INVALID_GITHUB_REPO",
            message="Could not parse GitHub repository identity.",
            status_code=422,
            detail={"repo": repo_value},
        ) from exc
    _raise_if_repo_identity_conflicts(canonical_repo=repo, request=request)
    _raise_if_forge_unsupported(repo)
    return repo, request.pr_number


def _raise_if_forge_unsupported(repo: RepoRef) -> None:
    """Reject a canonical ref on a forge AWF cannot adopt yet.

    Forge detection (issue #345) makes ``RepoRef.from_url`` accept non-GitHub
    hosts (e.g. ``bitbucket.org``) as ``RepoRef(forge="bitbucket")``. Adoption
    fetches PR metadata via the GitHub-only ``gh pr view --repo owner/repo``
    path, which silently targets GitHub for the same slug — so a non-GitHub ref
    must fail fast HERE, before any metadata fetch, rather than mis-route to
    GitHub (and the executor forge gate runs too late to catch it). Routes
    through :func:`ensure_forge_supported` so the supported-forge set stays a
    single source of truth.
    """
    try:
        ensure_forge_supported(repo.forge)
    except ForgeNotSupportedError as exc:
        raise PRMonitorAdoptionError(
            error_code=exc.reason_code,
            message=exc.message,
            status_code=422,
            detail={"repo_slug": repo.slug(), "forge": repo.forge},
        ) from exc


def _raise_if_pr_url_forge_unsupported(pr_url: str) -> None:
    """Surface FORGE_NOT_SUPPORTED for a well-formed PR URL on an unsupported forge.

    ``parse_github_pull_request_url`` only accepts ``github.com`` hosts; every
    other host raises a bare ``ValueError`` that the caller reads as
    PR_ADOPTION_INPUT_REQUIRED, so a Bitbucket ``pr_url`` would never reach
    :func:`_raise_if_forge_unsupported`. Re-parse the URL with the forge-aware
    ``RepoRef.from_url`` (which accepts e.g. ``bitbucket.org``) and route through
    the same gate, keeping FORGE_NOT_SUPPORTED reachable from the ``pr_url`` branch
    as the contract documents. A URL that even ``RepoRef.from_url`` cannot parse is
    genuinely malformed input — return so the caller raises PR_ADOPTION_INPUT_REQUIRED.
    """
    try:
        repo = RepoRef.from_url(pr_url)
    except ValueError:
        return
    _raise_if_forge_unsupported(repo)


def _raise_if_repo_identity_conflicts(
    *,
    canonical_repo: RepoRef,
    request: PullRequestMonitorAdoptionRequest,
) -> None:
    """Reject supplied repo identities that disagree with the canonical ref.

    Repo identity is ``(forge, owner, name)`` — not the ``owner/repo`` slug
    alone. Forge detection (issue #345) makes ``RepoRef.from_url`` parse a
    ``bitbucket.org`` ``repo_url`` as ``RepoRef(forge="bitbucket")`` with the
    *same* slug as a GitHub ``pr_url``. Comparing slug only would accept that
    inconsistent input; ``_adoption_repo_url`` would then persist the Bitbucket
    URL and the executor forge gate would fail the workspace too late. Compare
    the forge as well so a same-slug/different-forge identity is rejected up
    front, alongside :func:`_raise_if_forge_unsupported` which gates the
    canonical ref.
    """
    for field_name, repo_value in (
        ("repo_url", request.repo_url),
        ("repo_slug", request.repo_slug),
    ):
        if not repo_value:
            continue
        try:
            requested_repo = RepoRef.from_url(repo_value)
        except ValueError as exc:
            raise PRMonitorAdoptionError(
                error_code="INVALID_GITHUB_REPO",
                message="Could not parse GitHub repository identity.",
                status_code=422,
                detail={"repo": repo_value, "field": field_name},
            ) from exc
        if (
            requested_repo.forge != canonical_repo.forge
            or requested_repo.slug().lower() != canonical_repo.slug().lower()
        ):
            raise PRMonitorAdoptionError(
                error_code="PR_ADOPTION_INPUT_REQUIRED",
                message="PR adoption repository identities refer to different repositories.",
                status_code=422,
                detail={
                    "expected_repo_slug": canonical_repo.slug(),
                    "actual_repo_slug": requested_repo.slug(),
                    "expected_forge": canonical_repo.forge,
                    "actual_forge": requested_repo.forge,
                    "field": field_name,
                },
            )


def _adoption_task_policy(
    *,
    repo: RepoRef,
    metadata: PullRequestAdoptionMetadata,
    request: PullRequestMonitorAdoptionRequest,
    repo_url: str,
    lineage: dict[str, object] | None = None,
) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "task_kind": PR_ADOPTION_TASK_KIND,
        "pr_adoption": {
            "repo_slug": repo.slug(),
            "pr_number": metadata.number,
            "pr_url": metadata.url,
            "head_ref": metadata.head_ref,
            "head_repo_slug": metadata.head_repo_slug,
            "head_repo_url": _github_repo_url_like(repo_url, metadata.head_repo_slug),
            "base_ref": metadata.base_ref,
            "head_sha": metadata.head_sha,
            "base_sha": metadata.base_sha,
            "state": metadata.state,
            "is_draft": metadata.is_draft,
            "author": metadata.author,
            "title": metadata.title,
            "operator_reason": _redacted_optional_text(request.reason),
            "source": "existing_github_pr",
            "execution": _requested_execution_policy(request),
        },
    }
    if lineage is not None:
        policy["pr_adoption"]["lineage"] = lineage
    # Persist the raw tri-state auto-merge intent durably so the provisioner
    # resolves the final flag from it and idempotent replays compare the stable
    # intent (not the provisional-then-resolved column).
    policy[AUTO_MERGE_INTENT_POLICY_KEY] = request.auto_merge
    policy.update(_requested_agent_policy(request))
    return policy


def _requested_agent_policy(request: PullRequestMonitorAdoptionRequest) -> dict[str, str]:
    policy: dict[str, str] = {}
    if request.model is not None:
        policy["agent_model"] = request.model
    if request.effort is not None:
        policy["agent_effort"] = request.effort
    elif request.model is not None:
        agent_runtime = AgentRuntime(request.agent.value)
        defaults = defaults_with_model_overrides({agent_runtime: request.model})
        agent_defaults = defaults.get(agent_runtime)
        if agent_defaults is not None and agent_defaults.effort is not None:
            policy["agent_effort"] = agent_defaults.effort
    return policy


def _raise_if_unsupported_agent(request: PullRequestMonitorAdoptionRequest) -> None:
    from awf.service.provider_readiness import (
        is_launchable_agent,
        supported_launchable_agents,
    )

    if not is_launchable_agent(request.agent.value):
        supported_agents = supported_launchable_agents()
        supported = ", ".join(sorted(supported_agents))
        raise PRMonitorAdoptionError(
            error_code="UNSUPPORTED_AGENT_RUNTIME",
            message=f"Agent runtime {request.agent.value!r} is not supported for PR monitor adoption; supported runtimes: {supported}.",
            detail={
                "agent": request.agent.value,
                "supported_agents": list(supported_agents),
            },
        )


def _requested_execution_policy(request: PullRequestMonitorAdoptionRequest) -> dict[str, str]:
    return {"mode": request.execution.mode}


def _raise_if_hosted_delegation_unconfigured(
    request: PullRequestMonitorAdoptionRequest,
    settings: Settings,
) -> None:
    if request.execution.mode != "hosted":
        return
    try:
        service_settings = resolve_service_settings(settings)
        hosted_delegation_config_from_service_settings(service_settings, required=True)
    except HostedDelegationConfigError as exc:
        raise PRMonitorAdoptionError(
            error_code="HOSTED_DELEGATION_NOT_CONFIGURED",
            message=("Hosted PR monitor adoption requires configured hosted delegation settings."),
            detail=exc.detail(),
        ) from exc


def _adoption_task_prompt(
    *,
    repo: RepoRef,
    metadata: PullRequestAdoptionMetadata,
) -> str:
    return (
        f"AWF adopted existing PR {repo.slug()}#{metadata.number}. "
        "Do not reimplement the original task. The PR monitor will invoke "
        "the selected coding agent only for review comments, CI repair, or "
        "base synchronization work."
    )


def _adoption_external_id(*, repo_slug: str, pr_number: int) -> str:
    digest = hashlib.sha256(f"{repo_slug.lower()}#{pr_number}".encode()).hexdigest()
    return f"pr-adopt-{digest[:40]}"


def _effective_adoption_external_id(
    request: PullRequestMonitorAdoptionRequest,
    *,
    repo_slug: str,
    pr_number: int,
) -> str:
    if request.external_id is not None:
        return request.external_id
    return _adoption_external_id(repo_slug=repo_slug, pr_number=pr_number)


def _is_generated_adoption_external_id_lineage(
    *,
    existing_external_id: str | None,
    repo_slug: str,
    pr_number: int,
) -> bool:
    if existing_external_id is None:
        return False
    generated = _adoption_external_id(repo_slug=repo_slug, pr_number=pr_number)
    if existing_external_id == generated:
        return True
    prefix = f"{generated}:g"
    if not existing_external_id.startswith(prefix):
        return False
    suffix = existing_external_id[len(prefix) :]
    return suffix.isdigit()


def _adoption_external_id_policy_conflicts(
    workspace: Workspace,
    request: PullRequestMonitorAdoptionRequest,
    *,
    repo_slug: str,
    pr_number: int,
) -> bool:
    existing = workspace.task_external_id
    if request.external_id is not None:
        return existing != request.external_id
    return not _is_generated_adoption_external_id_lineage(
        existing_external_id=existing,
        repo_slug=repo_slug,
        pr_number=pr_number,
    )


def _task_external_id_conflict_error(exc: TaskExternalIdConflictError) -> PRMonitorAdoptionError:
    return PRMonitorAdoptionError(
        error_code="TASK_EXTERNAL_ID_CONFLICT",
        message=(
            "External task ID is already associated with a different "
            "repo/base/task-class/owned-path scope; use a unique external "
            "task ID for this backlog slice or retry the original scope."
        ),
        detail={"external_id": _redacted_optional_text(exc.external_id)},
    )


# Matches Workspace/Task idempotency_key and external_id String(128) columns.
_ADOPTION_EXTERNAL_ID_MAX_LENGTH = 128
_SUPERSEDED_EXTERNAL_ID_MARKER = ":superseded:"
# Prefer nonce 0, then salt until an unoccupied unique slot is found.
_SUPERSEDED_EXTERNAL_ID_ALLOCATION_ATTEMPTS = 32


def _encode_superseded_slot(
    *,
    base: str,
    workspace_id: str,
    collision_nonce: int = 0,
) -> str:
    """Encode a superseded identity slot that always fits String(128).

    The naive ``{base}:superseded:{workspace_id}`` form is preferred for short
    bases. Longer bases (or salted variants) use a digest prefix that still ends
    with ``:superseded:{workspace_id}`` (or a digest of it) for detection.

    ``collision_nonce`` > 0 salts the preferred/digest forms so allocation can
    dodge an already-occupied unique-constraint candidate.
    """
    if collision_nonce == 0:
        prefix = base
        digest_material = f"{base}\0{workspace_id}"
    else:
        prefix = f"{base}:c{collision_nonce}"
        digest_material = f"{base}\0{workspace_id}\0{collision_nonce}"
    candidate = f"{prefix}{_SUPERSEDED_EXTERNAL_ID_MARKER}{workspace_id}"
    if len(candidate) <= _ADOPTION_EXTERNAL_ID_MAX_LENGTH:
        return candidate
    digest = hashlib.sha256(digest_material.encode()).hexdigest()
    suffix = f"{_SUPERSEDED_EXTERNAL_ID_MARKER}{workspace_id}"
    if len(suffix) < _ADOPTION_EXTERNAL_ID_MAX_LENGTH:
        budget = _ADOPTION_EXTERNAL_ID_MAX_LENGTH - len(suffix)
        return f"{digest[:budget]}{suffix}"
    # Pathological workspace_id: fold both sides into a fixed-width encoding.
    workspace_digest = hashlib.sha256(workspace_id.encode()).hexdigest()
    return f"{digest[:40]}{_SUPERSEDED_EXTERNAL_ID_MARKER}{workspace_digest[:40]}"


def _superseded_adoption_idempotency_key(
    *,
    idempotency_key: str,
    workspace_id: str,
    collision_nonce: int = 0,
) -> str:
    """Return a superseded idempotency slot that always fits String(128)."""
    return _encode_superseded_slot(
        base=idempotency_key,
        workspace_id=workspace_id,
        collision_nonce=collision_nonce,
    )


def _superseded_adoption_external_id(
    *,
    external_id: str,
    workspace_id: str,
    collision_nonce: int = 0,
) -> str:
    """Return a unique superseded slot that always fits the external_id column.

    The naive ``{id}:superseded:{workspace_id}`` form is preferred for short IDs.
    Explicit IDs may be up to 128 characters; appending the marker and workspace
    id then overflows ``String(128)``, so long IDs use a digest prefix that still
    ends with ``:superseded:{workspace_id}`` (or a digest of it) for detection
    and remains unique per pair.

    ``collision_nonce`` > 0 salts the preferred/digest forms so allocation can
    dodge an already-occupied ``uq_tasks_external_id`` candidate.
    """
    return _encode_superseded_slot(
        base=external_id,
        workspace_id=workspace_id,
        collision_nonce=collision_nonce,
    )


def _is_superseded_adoption_external_id(external_id: str, *, workspace_id: str) -> bool:
    """True when *external_id* is the supersession slot encoded for *workspace_id*.

    Caller-controlled IDs may embed the ``:superseded:`` substring. Only IDs that
    match this workspace's encoding are treated as already released, so a later
    re-adoption can free the original slot instead of hitting TASK_EXTERNAL_ID_CONFLICT.
    """
    suffix = f"{_SUPERSEDED_EXTERNAL_ID_MARKER}{workspace_id}"
    if len(suffix) < _ADOPTION_EXTERNAL_ID_MAX_LENGTH:
        return external_id.endswith(suffix)
    workspace_digest = hashlib.sha256(workspace_id.encode()).hexdigest()
    return external_id.endswith(f"{_SUPERSEDED_EXTERNAL_ID_MARKER}{workspace_digest[:40]}")


async def _task_external_id_occupied(session: AsyncSession, external_id: str) -> bool:
    stmt = select(Task.id).where(Task.external_id == external_id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _workspace_idempotency_key_occupied(session: AsyncSession, key: str) -> bool:
    stmt = select(Workspace.id).where(Workspace.idempotency_key == key).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _task_idempotency_key_occupied(session: AsyncSession, key: str) -> bool:
    stmt = select(Task.id).where(Task.idempotency_key == key).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _superseded_adoption_idempotency_key_occupied(
    session: AsyncSession,
    key: str,
) -> bool:
    """True when *key* is taken under either idempotency unique constraint."""
    return await _workspace_idempotency_key_occupied(
        session, key
    ) or await _task_idempotency_key_occupied(session, key)


async def _allocate_superseded_adoption_idempotency_key(
    session: AsyncSession,
    *,
    idempotency_key: str,
    workspace_id: str,
) -> str:
    """Return an unoccupied superseded idempotency key for *workspace_id*.

    Prefers the deterministic nonce-0 encoding; if another workspace or task
    already owns that value, try salted candidates rather than flushing into
    ``uq_workspaces_idempotency_key`` / ``uq_tasks_idempotency_key``.

    The occupancy probe is best-effort: a concurrent insert can still claim the
    candidate before the caller's flush. ``_supersede_previous_adoption`` recovers
    that race inside a savepoint and retries allocation.
    """
    for collision_nonce in range(_SUPERSEDED_EXTERNAL_ID_ALLOCATION_ATTEMPTS):
        candidate = _superseded_adoption_idempotency_key(
            idempotency_key=idempotency_key,
            workspace_id=workspace_id,
            collision_nonce=collision_nonce,
        )
        if not await _superseded_adoption_idempotency_key_occupied(session, candidate):
            return candidate
    raise PRMonitorAdoptionError(
        error_code="TASK_EXTERNAL_ID_CONFLICT",
        message=(
            "Unable to allocate a free superseded idempotency key slot; "
            "retry after clearing colliding idempotency keys."
        ),
        detail={"workspace_id": workspace_id},
    )


async def _allocate_superseded_adoption_external_id(
    session: AsyncSession,
    *,
    external_id: str,
    workspace_id: str,
) -> str:
    """Return an unoccupied superseded external_id for *workspace_id*.

    Prefers the deterministic nonce-0 encoding; if another task already owns that
    value, try salted candidates rather than flushing into ``uq_tasks_external_id``.

    The occupancy probe is best-effort: a concurrent insert can still claim the
    candidate before the caller's flush. ``_supersede_previous_adoption`` recovers
    that ``uq_tasks_external_id`` race inside a savepoint and retries allocation.
    """
    for collision_nonce in range(_SUPERSEDED_EXTERNAL_ID_ALLOCATION_ATTEMPTS):
        candidate = _superseded_adoption_external_id(
            external_id=external_id,
            workspace_id=workspace_id,
            collision_nonce=collision_nonce,
        )
        if not await _task_external_id_occupied(session, candidate):
            return candidate
    # Practically unreachable with salted digests; surface as a domain conflict
    # instead of an untranslated IntegrityError on flush.
    raise PRMonitorAdoptionError(
        error_code="TASK_EXTERNAL_ID_CONFLICT",
        message=(
            "Unable to allocate a free superseded external task ID slot; "
            "retry after clearing colliding external task IDs."
        ),
        detail={"workspace_id": workspace_id},
    )


async def _release_superseded_adoption_external_id(
    session: AsyncSession,
    external_id: str | None,
    *,
    workspace_id: str,
) -> str | None:
    """Rewrite an active adoption external ID into a free superseded slot.

    Values already encoded for *workspace_id* (and ``None``) are left unchanged
    so a second supersession pass does not nest ``:superseded:`` markers.
    """
    if external_id is None or _is_superseded_adoption_external_id(
        external_id,
        workspace_id=workspace_id,
    ):
        return external_id
    return await _allocate_superseded_adoption_external_id(
        session,
        external_id=external_id,
        workspace_id=workspace_id,
    )


def _adoption_generation_external_id(
    *,
    repo_slug: str,
    pr_number: int,
    logical_idempotency_key: str,
    workspace_idempotency_key: str,
) -> str:
    base_external_id = _adoption_external_id(repo_slug=repo_slug, pr_number=pr_number)
    generation = _adoption_generation_suffix(
        logical_idempotency_key=logical_idempotency_key,
        workspace_idempotency_key=workspace_idempotency_key,
    )
    return f"{base_external_id}:{generation}"


def _adoption_generation_suffix(
    *,
    logical_idempotency_key: str,
    workspace_idempotency_key: str,
) -> str:
    prefix = f"{logical_idempotency_key}:"
    if workspace_idempotency_key.startswith(prefix):
        return workspace_idempotency_key[len(prefix) :]
    return "g1"


def _adoption_repo_url(*, request: PullRequestMonitorAdoptionRequest, repo: RepoRef) -> str:
    return request.repo_url or repo.ssh_url()


def _github_repo_url_like(repo_url: str, repo_slug: str) -> str:
    return RepoRef.from_url(repo_slug).clone_url_like(repo_url)


def _adoption_workspace_is_resumable(workspace: Workspace) -> bool:
    if workspace.status == "superseded":
        return False
    try:
        status = WorkspaceStatus(workspace.status)
    except ValueError:
        # Unknown statuses may be active states introduced before this code
        # was updated; attach rather than superseding the canonical key.
        return True
    return status not in _NON_RESUMABLE_ADOPTION_STATUSES


def _workspace_status_for_response(status: str) -> WorkspaceStatus | str:
    try:
        return WorkspaceStatus(status)
    except ValueError:
        return status


def _adoption_workspace_forge(workspace: Workspace) -> str | None:
    """Recover the forge of an adopted workspace from its persisted repo URL.

    ``_adoption_repo_url`` always persists a parseable URL (the request URL or
    ``repo.ssh_url()``), so ``RepoRef.from_url`` recovers the forge. Return
    ``None`` for an unparseable legacy URL so the caller falls back to the prior
    forge-agnostic behavior rather than rejecting a resumable adoption.
    """
    try:
        return RepoRef.from_url(workspace.repo_url).forge
    except ValueError:
        return None


def _raise_if_adoption_forge_mismatch(workspace: Workspace, *, repo: RepoRef) -> None:
    """Reject attaching a request to an adoption on a different forge.

    Repo identity is ``(forge, owner, name)``, but the adoption idempotency key
    and history lookup are keyed on the forge-agnostic ``owner/repo`` slug. After
    issue #345 flipped bitbucket into the supported-forge set, a ``bitbucket.org``
    request for the same slug/PR would otherwise attach to an existing GitHub
    adoption *before* ``_fetch_metadata`` (and the GitHub-only default-fetcher
    gate) ever runs. Fail closed here so a same-slug/different-forge request never
    silently inherits a different repository's monitor. (Letting same-slug
    different-forge adoptions coexist would require forge-qualified identity keys
    — follow-up work.)
    """
    existing_forge = _adoption_workspace_forge(workspace)
    if existing_forge is not None and existing_forge != repo.forge:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message=(
                "Canonical PR adoption idempotency key is already owned by a "
                "workspace on a different forge."
            ),
            detail={
                "workspace_id": workspace.id,
                "repo_slug": repo.slug(),
                "requested_forge": repo.forge,
                "existing_forge": existing_forge,
            },
        )


def _raise_if_existing_workspace_is_not_requested_adoption(
    workspace: Workspace,
    *,
    repo: RepoRef,
    pr_number: int,
) -> None:
    adoption = _adoption_policy(workspace)
    existing_repo_slug = _optional_str(adoption.get("repo_slug"))
    existing_pr_number = _optional_int(adoption.get("pr_number"))
    if (
        existing_repo_slug is not None
        and existing_repo_slug.lower() == repo.slug().lower()
        and existing_pr_number == pr_number
    ):
        return

    # History matching requires structured pr_adoption identity, so legacy rows
    # with None / non-object task_policy fall through to this idempotency-key
    # path. Accept only with independent persisted column proof of the same
    # forge/repo/PR — never on task_kind / external_id / key alone.
    if not adoption and _legacy_workspace_matches_requested_pr_identity(
        workspace,
        repo=repo,
        pr_number=pr_number,
    ):
        return

    raise PRMonitorAdoptionError(
        error_code="PR_ADOPTION_POLICY_CONFLICT",
        message=(
            "Canonical PR adoption idempotency key is already owned by a workspace "
            "for a different or missing PR adoption identity."
        ),
        detail={
            "workspace_id": workspace.id,
            "repo_slug": repo.slug(),
            "pr_number": pr_number,
            "existing_task_kind": workspace.task_kind,
            "existing_pr_adoption_repo_slug": existing_repo_slug,
            "existing_pr_adoption_pr_number": existing_pr_number,
        },
    )


def _legacy_workspace_matches_requested_pr_identity(
    workspace: Workspace,
    *,
    repo: RepoRef,
    pr_number: int,
) -> bool:
    """True when persisted workspace columns independently prove PR identity.

    Used only for legacy rows missing structured ``pr_adoption`` policy. Fail
    closed on absent, malformed, or mismatched ``pr_url`` / ``pr_number`` /
    ``repo_url``.
    """
    if workspace.task_kind != PR_ADOPTION_TASK_KIND:
        return False
    if workspace.pr_number != pr_number:
        return False
    pr_url = workspace.pr_url
    if not isinstance(pr_url, str) or not pr_url.strip():
        return False
    try:
        parsed_repo, parsed_pr = parse_github_pull_request_url(pr_url)
    except ValueError:
        return False
    if parsed_pr != pr_number:
        return False
    if parsed_repo.forge != repo.forge or parsed_repo.slug().lower() != repo.slug().lower():
        return False
    try:
        workspace_repo = RepoRef.from_url(workspace.repo_url)
    except ValueError:
        return False
    return (
        workspace_repo.forge == repo.forge and workspace_repo.slug().lower() == repo.slug().lower()
    )


def _raise_if_policy_conflicts(
    workspace: Workspace,
    request: PullRequestMonitorAdoptionRequest,
    *,
    repo: RepoRef,
    pr_number: int,
) -> None:
    requested_grace = request.initial_review_grace_period_seconds
    requested_agent = request.agent.value
    requested_profile = _requested_inline_profile_policy(request)
    requested_repo_url = request.repo_url
    if requested_repo_url is not None and workspace.repo_url != requested_repo_url:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different repo_url policy.",
            detail={
                "workspace_id": workspace.id,
                "repo_slug": repo.slug(),
                "existing_repo_url": workspace.repo_url,
                "requested_repo_url": requested_repo_url,
            },
        )
    if workspace.agent != requested_agent:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different agent policy.",
            detail={
                "workspace_id": workspace.id,
                "existing_agent": workspace.agent,
                "requested_agent": requested_agent,
            },
        )
    existing_execution = pr_adoption_execution_policy(workspace.task_policy)
    requested_execution = _requested_execution_policy(request)
    if existing_execution != requested_execution:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different execution policy.",
            detail={
                "workspace_id": workspace.id,
                "existing_execution": existing_execution,
                "requested_execution": requested_execution,
            },
        )
    _raise_if_agent_policy_conflicts(workspace, request)
    if workspace.profile_ref != request.profile_ref:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different profile_ref policy.",
            detail={
                "workspace_id": workspace.id,
                "existing_profile_ref": workspace.profile_ref,
                "requested_profile_ref": request.profile_ref,
            },
        )
    if normalize_inline_profile_snapshot(
        workspace.requested_profile
    ) != normalize_inline_profile_snapshot(requested_profile):
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different inline profile policy.",
            detail={
                "workspace_id": workspace.id,
                "existing_inline_profile_name": _inline_profile_name(workspace.requested_profile),
                "requested_inline_profile_name": _inline_profile_name(requested_profile),
            },
        )
    if set(workspace.owned_paths) != set(request.owned_paths):
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different owned_paths policy.",
            detail={
                "workspace_id": workspace.id,
                "existing_owned_paths": list(workspace.owned_paths),
                "requested_owned_paths": list(request.owned_paths),
            },
        )
    if workspace.task_tag != request.task_tag:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different task_tag policy.",
            detail={
                "workspace_id": workspace.id,
                "existing_task_tag": workspace.task_tag,
                "requested_task_tag": request.task_tag,
            },
        )
    if _adoption_external_id_policy_conflicts(
        workspace,
        request,
        repo_slug=repo.slug(),
        pr_number=pr_number,
    ):
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different external_id policy.",
            detail={
                "workspace_id": workspace.id,
                "field": "external_id",
                "existing_external_id": _redacted_optional_text(workspace.task_external_id),
                "requested_external_id": _redacted_optional_text(
                    _effective_adoption_external_id(
                        request,
                        repo_slug=repo.slug(),
                        pr_number=pr_number,
                    )
                    if request.external_id is not None
                    else None
                ),
                "requested_external_id_policy": (
                    "explicit" if request.external_id is not None else "generated"
                ),
            },
        )
    requested_task_class = request.task_class.value if request.task_class is not None else None
    if workspace.task_class != requested_task_class:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different task_class policy.",
            detail={
                "workspace_id": workspace.id,
                "field": "task_class",
                "existing_task_class": workspace.task_class,
                "requested_task_class": requested_task_class,
            },
        )
    # Compare the persisted tri-state INTENT, not the resolved column: the
    # resolved value can legitimately differ from a None intent (profile/default),
    # so a replay with the same None intent must not spuriously conflict. Legacy
    # rows written before the intent key existed reconstruct the historical intent
    # from the persisted column (see ``_adoption_auto_merge_conflicts``).
    if _adoption_auto_merge_conflicts(workspace, request):
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different auto_merge policy.",
            detail={
                "workspace_id": workspace.id,
                "existing_auto_merge": _adoption_auto_merge_intent(workspace),
                "requested_auto_merge": request.auto_merge,
            },
        )
    if requested_grace != workspace.initial_review_grace_period_seconds:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message=("Existing adopted PR monitor uses a different initial review grace policy."),
            detail={
                "workspace_id": workspace.id,
                "existing_initial_review_grace_period_seconds": (
                    workspace.initial_review_grace_period_seconds
                ),
                "requested_initial_review_grace_period_seconds": requested_grace,
            },
        )


def _adoption_auto_merge_intent(workspace: Workspace) -> bool | None:
    """Reconstruct the adoption's tri-state auto-merge intent for idempotency.

    New-world rows persist the intent under ``AUTO_MERGE_INTENT_POLICY_KEY``; return
    it verbatim. Legacy rows written before the key existed have none, and the
    historical adoption request default was ``True`` (omitted -> merge) with the
    persisted column set straight from the request, so reconstruct the historical
    intent from the column (``auto_merge is not False`` -> ``True``). This value is
    reported in the conflict detail so it reflects the compared intent, not ``None``.
    """
    policy = workspace.task_policy
    if isinstance(policy, Mapping) and AUTO_MERGE_INTENT_POLICY_KEY in policy:
        return auto_merge_intent_from_policy(policy)
    return getattr(workspace, "auto_merge", None) is not False


def _adoption_auto_merge_conflicts(
    workspace: Workspace,
    request: PullRequestMonitorAdoptionRequest,
) -> bool:
    """Whether a replay's auto-merge intent conflicts with the stored adoption.

    New-world rows (intent key present) compare the persisted intent strictly
    against the request intent. Legacy rows (no intent key) reconstruct the
    historical intent from the persisted column and treat an omitted (``None``)
    replay as that same historical default (``True``), so a pre-change row created
    by an omitted/True request stays idempotent against a post-change replay instead
    of spuriously raising ``PR_ADOPTION_POLICY_CONFLICT``.
    """
    policy = workspace.task_policy
    persisted_intent = _adoption_auto_merge_intent(workspace)
    if isinstance(policy, Mapping) and AUTO_MERGE_INTENT_POLICY_KEY in policy:
        return persisted_intent != request.auto_merge
    effective_request = True if request.auto_merge is None else request.auto_merge
    return persisted_intent != effective_request


def _raise_if_agent_policy_conflicts(
    workspace: Workspace,
    request: PullRequestMonitorAdoptionRequest,
) -> None:
    existing_policy = _workspace_agent_policy(workspace)
    requested_policy = _requested_agent_policy(request)
    for key in ("agent_model", "agent_effort"):
        existing_value = existing_policy.get(key)
        requested_value = requested_policy.get(key)
        if existing_value == requested_value:
            continue
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message=f"Existing adopted PR monitor uses a different {key} policy.",
            detail={
                "workspace_id": workspace.id,
                f"existing_{key}": existing_value,
                f"requested_{key}": requested_value,
            },
        )


def _workspace_agent_policy(workspace: Workspace) -> dict[str, str]:
    policy: object = workspace.task_policy
    if not isinstance(policy, Mapping):
        return {}
    agent_policy: dict[str, str] = {}
    model = _optional_str(policy.get("agent_model"))
    effort = _optional_str(policy.get("agent_effort"))
    if model is not None:
        agent_policy["agent_model"] = model
    if effort is not None:
        agent_policy["agent_effort"] = effort
    return agent_policy


def _requested_inline_profile_policy(
    request: PullRequestMonitorAdoptionRequest,
) -> dict[str, Any] | None:
    return (
        request.profile.model_dump(mode="json", by_alias=True)
        if request.profile is not None
        else None
    )


def _inline_profile_name(profile: Mapping[str, Any] | None) -> str | None:
    if profile is None:
        return None
    name = profile.get("name")
    return name if isinstance(name, str) else None


def _adoption_policy(workspace: Workspace) -> Mapping[str, Any]:
    policy = workspace.task_policy
    adoption = policy.get("pr_adoption") if isinstance(policy, dict) else None
    return adoption if isinstance(adoption, Mapping) else {}


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _redacted_optional_text(value: str | None) -> str | None:
    return redact_audit_text(value) if value else None


def _metadata_error_status_code(reason_code: str) -> int:
    if reason_code == "PR_NOT_FOUND":
        return 404
    if reason_code in {"PR_ALREADY_CLOSED", "PR_ALREADY_MERGED"}:
        return 409
    if reason_code in {"INVALID_GITHUB_REPO", "PR_ADOPTION_INPUT_REQUIRED"}:
        return 422
    return 502


__all__ = (
    "PR_ADOPTION_REQUESTED_EVENT_TYPE",
    "PR_ADOPTION_SUPERSEDED_EVENT_TYPE",
    "PR_ADOPTION_REQUESTED_REASON",
    "PR_ADOPTION_SUPERSEDED_REASON",
    "PR_ADOPTION_ADMITTED_REASON",
    "PR_ADOPTION_OPERATION_ACTION",
    "PR_ADOPTION_TASK_KIND",
    "PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY",
    "_LIVE_ADOPTION_STATUSES",
    "_PR_ADOPTION_ERROR_CODE_CONTRACT",
    "_NON_RESUMABLE_ADOPTION_STATUSES",
    "_log",
    "MetadataFetcher",
    "PRMonitorAdoptionError",
    "_default_metadata_fetcher",
    "_select_live_adoption_workspace",
    "_is_live_adoption_status",
    "_next_adoption_workspace_idempotency_key",
    "_task_idempotency_key_family",
    "_task_external_id_family_idempotency_keys",
    "_next_adoption_task_idempotency_key",
    "_task_has_existing_attempt",
    "_task_has_shared_ownership_attempt",
    "_adoption_owns_task_identity",
    "_is_adoption_generation_suffix",
    "_terminal_adoption_lineage",
    "_adoption_lineage_payload",
    "pr_adoption_idempotency_key",
    "_normalize_request_identity",
    "_raise_if_forge_unsupported",
    "_raise_if_pr_url_forge_unsupported",
    "_raise_if_repo_identity_conflicts",
    "_adoption_task_policy",
    "_requested_agent_policy",
    "_requested_execution_policy",
    "_raise_if_hosted_delegation_unconfigured",
    "_adoption_task_prompt",
    "_adoption_external_id",
    "_effective_adoption_external_id",
    "_is_generated_adoption_external_id_lineage",
    "_adoption_external_id_policy_conflicts",
    "_task_external_id_conflict_error",
    "_superseded_adoption_idempotency_key",
    "_encode_superseded_slot",
    "_ADOPTION_EXTERNAL_ID_MAX_LENGTH",
    "_SUPERSEDED_EXTERNAL_ID_MARKER",
    "_SUPERSEDED_EXTERNAL_ID_ALLOCATION_ATTEMPTS",
    "_superseded_adoption_external_id",
    "_is_superseded_adoption_external_id",
    "_task_external_id_occupied",
    "_workspace_idempotency_key_occupied",
    "_task_idempotency_key_occupied",
    "_superseded_adoption_idempotency_key_occupied",
    "_allocate_superseded_adoption_idempotency_key",
    "_allocate_superseded_adoption_external_id",
    "_release_superseded_adoption_external_id",
    "_adoption_generation_external_id",
    "_adoption_generation_suffix",
    "_adoption_repo_url",
    "_github_repo_url_like",
    "_adoption_workspace_is_resumable",
    "_workspace_status_for_response",
    "_adoption_workspace_forge",
    "_raise_if_adoption_forge_mismatch",
    "_raise_if_existing_workspace_is_not_requested_adoption",
    "_legacy_workspace_matches_requested_pr_identity",
    "_raise_if_policy_conflicts",
    "_adoption_auto_merge_intent",
    "_adoption_auto_merge_conflicts",
    "_raise_if_agent_policy_conflicts",
    "_workspace_agent_policy",
    "_requested_inline_profile_policy",
    "_inline_profile_name",
    "_adoption_policy",
    "_optional_str",
    "_optional_int",
    "_redacted_optional_text",
    "_metadata_error_status_code",
    "_raise_if_unsupported_agent",
)
