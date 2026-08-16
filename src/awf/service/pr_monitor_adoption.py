"""First-class adoption of existing GitHub PRs into AWF monitoring."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from sqlalchemy import inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.adapters.defaults import defaults_with_model_overrides
from awf.api.schemas import (
    PullRequestMonitorAdoptionRequest,
    PullRequestMonitorAdoptionResponse,
)
from awf.common.audit import redact_audit_text
from awf.common.auto_merge import (
    AUTO_MERGE_INTENT_POLICY_KEY,
    auto_merge_intent_from_policy,
    auto_merge_is_resolved,
    seed_auto_merge,
)
from awf.common.commands import AsyncioSubprocessRunner
from awf.common.config import Settings, get_settings
from awf.common.forge import ForgeNotSupportedError, ensure_forge_supported
from awf.common.github_client import (
    PullRequestAdoptionMetadata,
    PullRequestMetadataError,
    RepoRef,
    fetch_pull_request_adoption_metadata,
    parse_github_pull_request_url,
)
from awf.common.logging import get_logger
from awf.common.workspace_policy import pr_adoption_execution_policy
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import MergeCandidate, Task, TaskAttempt, Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskExternalIdConflictError,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.utils import escape_like_pattern as _escape_like_pattern
from awf.profiles.models import normalize_inline_profile_snapshot
from awf.runtime.hosted_delegation import (
    HostedDelegationConfigError,
)
from awf.service.config import (
    hosted_delegation_config_from_service_settings,
    resolve_service_settings,
)
from awf.service.node_identity import effective_worker_node_id
from awf.service.scheduler import scheduler_score_from_workspace
from awf.service.validation_observability import validation_freshness_summary

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


class PullRequestMonitorAdoptionService:
    """Shared domain service for REST, CLI-backed scripts, and MCP."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        metadata_fetcher: Callable[..., Awaitable[PullRequestAdoptionMetadata]] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._metadata_fetcher = metadata_fetcher or _default_metadata_fetcher

    async def adopt(
        self,
        request: PullRequestMonitorAdoptionRequest,
    ) -> PullRequestMonitorAdoptionResponse:
        repo, pr_number = _normalize_request_identity(request)
        _raise_if_hosted_delegation_unconfigured(request, self._settings)
        idempotency_key = pr_adoption_idempotency_key(
            repo_slug=repo.slug(),
            pr_number=pr_number,
        )
        # History lookup stays on the deterministic generated adoption id even when
        # the caller supplies an explicit external_id (policy/persistence only).
        history_external_id = _adoption_external_id(repo_slug=repo.slug(), pr_number=pr_number)
        effective_external_id = _effective_adoption_external_id(
            request,
            repo_slug=repo.slug(),
            pr_number=pr_number,
        )
        workspace_repo = WorkspaceRepository(self._session)
        await workspace_repo.acquire_idempotency_key_lock(idempotency_key)
        # Re-read under the transaction lock so a concurrent adopter that won
        # the race attaches here instead of surfacing the unique constraint.
        adoption_history = await workspace_repo.list_pr_adoption_history(
            task_external_id=history_external_id,
            idempotency_key=idempotency_key,
            task_kind=PR_ADOPTION_TASK_KIND,
            repo_slug=repo.slug(),
            pr_number=pr_number,
        )
        live_adoption = _select_live_adoption_workspace(adoption_history)
        if live_adoption is not None:
            _raise_if_adoption_forge_mismatch(live_adoption, repo=repo)
            _raise_if_policy_conflicts(live_adoption, request, repo=repo, pr_number=pr_number)
            return await self._response(live_adoption, attached_existing=True)

        existing = await workspace_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            _raise_if_adoption_forge_mismatch(existing, repo=repo)
            _raise_if_existing_workspace_is_not_requested_adoption(
                existing,
                repo=repo,
                pr_number=pr_number,
            )
            if _adoption_workspace_is_resumable(existing):
                _raise_if_policy_conflicts(existing, request, repo=repo, pr_number=pr_number)
                return await self._response(existing, attached_existing=True)

        metadata = await self._fetch_metadata(repo=repo, pr_number=pr_number)
        previous_terminal_adoptions = await _terminal_adoption_lineage(
            self._session,
            adoption_history,
        )
        superseded_adoption: dict[str, Any] | None = None
        superseded_workspace: Workspace | None = None
        if existing is not None:
            superseded_adoption = await self._supersede_previous_adoption(
                workspace=existing,
                idempotency_key=idempotency_key,
                repo=repo,
                pr_number=pr_number,
                effective_external_id=effective_external_id,
            )
            superseded_workspace = existing
        try:
            workspace = await self._create_adoption_workspace(
                request=request,
                repo=repo,
                metadata=metadata,
                idempotency_key=idempotency_key,
                logical_idempotency_key=idempotency_key,
                previous_terminal_adoptions=previous_terminal_adoptions,
                superseded_adoption=superseded_adoption,
                superseded_workspace=superseded_workspace,
                effective_external_id=effective_external_id,
            )
        except TaskExternalIdConflictError as exc:
            raise _task_external_id_conflict_error(exc) from exc
        return await self._response(workspace, attached_existing=False)

    async def _supersede_previous_adoption(
        self,
        *,
        workspace: Workspace,
        idempotency_key: str,
        repo: RepoRef,
        pr_number: int,
        effective_external_id: str,
    ) -> dict[str, Any]:
        previous_idempotency_key = workspace.idempotency_key
        superseded_idempotency_key = _superseded_adoption_idempotency_key(
            idempotency_key=idempotency_key,
            workspace_id=workspace.id,
        )
        workspace.idempotency_key = superseded_idempotency_key
        superseded_external_id = _superseded_adoption_external_id(
            external_id=effective_external_id,
            workspace_id=workspace.id,
        )
        if workspace.task_external_id == effective_external_id:
            workspace.task_external_id = superseded_external_id

        attempt = await TaskAttemptRepository(self._session).get_by_workspace_id(workspace.id)
        if attempt is not None:
            task = await TaskRepository(self._session).get(attempt.task_id)
            if task is not None:
                if task.idempotency_key == idempotency_key:
                    task.idempotency_key = superseded_idempotency_key
                if task.external_id == effective_external_id:
                    task.external_id = superseded_external_id

        await WorkspaceRepository(self._session).advance_workspace_version(workspace)
        await self._session.flush()
        return {
            "reason_code": PR_ADOPTION_SUPERSEDED_REASON,
            "repo_slug": repo.slug(),
            "pr_number": pr_number,
            "previous_workspace_id": workspace.id,
            "previous_status": workspace.status,
            "previous_idempotency_key": previous_idempotency_key,
            "superseded_idempotency_key": superseded_idempotency_key,
        }

    async def _fetch_metadata(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
    ) -> PullRequestAdoptionMetadata:
        try:
            metadata = await self._metadata_fetcher(repo=repo, pr_number=pr_number)
        except PullRequestMetadataError as exc:
            raise PRMonitorAdoptionError(
                error_code=exc.reason_code,
                message=exc.message,
                status_code=_metadata_error_status_code(exc.reason_code),
                detail=exc.detail,
            ) from exc
        if metadata.merged:
            raise PRMonitorAdoptionError(
                error_code="PR_ALREADY_MERGED",
                message=f"PR {repo.slug()}#{pr_number} is already merged.",
                detail={
                    "repo_slug": repo.slug(),
                    "pr_number": pr_number,
                    "state": metadata.state,
                },
            )
        if metadata.closed:
            raise PRMonitorAdoptionError(
                error_code="PR_ALREADY_CLOSED",
                message=f"PR {repo.slug()}#{pr_number} is closed.",
                detail={
                    "repo_slug": repo.slug(),
                    "pr_number": pr_number,
                    "state": metadata.state,
                },
            )
        return metadata

    async def _create_adoption_workspace(
        self,
        *,
        request: PullRequestMonitorAdoptionRequest,
        repo: RepoRef,
        metadata: PullRequestAdoptionMetadata,
        idempotency_key: str,
        logical_idempotency_key: str,
        previous_terminal_adoptions: list[dict[str, str | None]],
        superseded_adoption: dict[str, Any] | None = None,
        superseded_workspace: Workspace | None = None,
        effective_external_id: str,
    ) -> Workspace:
        requested_profile = _requested_inline_profile_policy(request)
        repo_url = _adoption_repo_url(request=request, repo=repo)
        task_policy = _adoption_task_policy(
            repo=repo,
            metadata=metadata,
            request=request,
            repo_url=repo_url,
            lineage=_adoption_lineage_payload(
                logical_idempotency_key=logical_idempotency_key,
                previous_terminal_adoptions=previous_terminal_adoptions,
            ),
        )
        operator_reason = _redacted_optional_text(request.reason)
        task_class = request.task_class.value if request.task_class is not None else None
        explicit_external_id = request.external_id is not None
        workspace_repo = WorkspaceRepository(self._session)
        workspace = await workspace_repo.create(
            repo_url=repo_url,
            branch_base=metadata.base_ref,
            task_title=(
                request.task_title
                or metadata.title
                or f"PR monitor adoption: {repo.slug()}#{metadata.number}"
            ),
            task_prompt=request.task_prompt or _adoption_task_prompt(repo=repo, metadata=metadata),
            task_external_id=effective_external_id,
            task_class=task_class,
            task_tag=request.task_tag,
            agent=request.agent.value,
            test_commands=[],
            requires_database=False,
            owned_paths=list(request.owned_paths),
            task_policy=task_policy,
            auto_merge=seed_auto_merge(request.auto_merge),
            initial_review_grace_period_seconds=request.initial_review_grace_period_seconds,
            profile_ref=request.profile_ref,
            requested_profile=requested_profile,
            idempotency_key=idempotency_key,
            task_kind=PR_ADOPTION_TASK_KIND,
            remote_push_branch=metadata.head_ref,
        )
        workspace.pr_url = metadata.url
        workspace.pr_number = metadata.number
        workspace.base_commit = metadata.base_sha
        workspace.monitor_last_commit_sha = metadata.head_sha
        superseded_payload = (
            {
                **superseded_adoption,
                "replacement_workspace_id": workspace.id,
            }
            if superseded_adoption is not None
            else None
        )

        task_repo = TaskRepository(self._session)

        async def _create_or_get_task(
            *,
            external_id: str | None,
            task_idempotency_key: str | None,
        ) -> Task:
            return await task_repo.create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=external_id,
                idempotency_key=task_idempotency_key,
                task_class=workspace.task_class,
                owned_paths=list(workspace.owned_paths),
            )

        async def _create_generated_task() -> Task:
            task_external_id = workspace.task_external_id or _adoption_external_id(
                repo_slug=repo.slug(),
                pr_number=metadata.number,
            )
            task_generation_idempotency_key = await _next_adoption_task_idempotency_key(
                self._session,
                logical_idempotency_key=logical_idempotency_key,
                task_external_id=task_external_id,
            )
            workspace.task_external_id = _adoption_generation_external_id(
                repo_slug=repo.slug(),
                pr_number=metadata.number,
                logical_idempotency_key=logical_idempotency_key,
                workspace_idempotency_key=task_generation_idempotency_key,
            )
            return await _create_or_get_task(
                external_id=workspace.task_external_id,
                task_idempotency_key=task_generation_idempotency_key,
            )

        try:
            task = await _create_or_get_task(
                external_id=workspace.task_external_id,
                task_idempotency_key=idempotency_key,
            )
        except TaskExternalIdConflictError:
            if explicit_external_id or not previous_terminal_adoptions:
                raise
            task = await _create_generated_task()
        else:
            if (
                previous_terminal_adoptions
                and task.idempotency_key == logical_idempotency_key
                and await _task_has_existing_attempt(self._session, task.id)
            ):
                if explicit_external_id:
                    raise TaskExternalIdConflictError(effective_external_id)
                task = await _create_generated_task()
        attempt = await TaskAttemptRepository(self._session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        node_id = effective_worker_node_id(self._settings)
        hosted_execution = request.execution.mode == "hosted"
        steady_cpu = 0.0 if hosted_execution else self._settings.workspace_steady_cpu
        steady_memory_gb = 0.0 if hosted_execution else self._settings.workspace_steady_memory_gb
        peak_cpu = 0.0 if hosted_execution else self._settings.workspace_peak_cpu
        peak_memory_gb = 0.0 if hosted_execution else self._settings.workspace_peak_memory_gb
        resource_summary: dict[str, Any] = {
            "node_id": node_id,
            "steady_cpu": steady_cpu,
            "steady_memory_gb": steady_memory_gb,
            "peak_cpu": peak_cpu,
            "peak_memory_gb": peak_memory_gb,
            "disk_mb": None,
            "dind_slots": 0,
            "phase": "workspace_lifecycle",
            "dind_mode": "none",
        }
        await ResourceReservationRepository(self._session).create(
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            node_id=node_id,
            steady_cpu=steady_cpu,
            steady_memory_gb=steady_memory_gb,
            peak_cpu=peak_cpu,
            peak_memory_gb=peak_memory_gb,
            disk_mb=None,
            dind_slots=0,
            phase="workspace_lifecycle",
        )
        scheduler_score = scheduler_score_from_workspace(workspace, now=workspace.created_at)
        await QueueDecisionRepository(self._session).create(
            workspace_id=workspace.id,
            task_id=task.id,
            attempt_id=attempt.id,
            decision="admitted",
            reason_code=PR_ADOPTION_ADMITTED_REASON,
            class_priority=scheduler_score.class_priority,
            computed_priority=scheduler_score.effective_score,
            age_boost=scheduler_score.age_boost,
            retry_bonus=scheduler_score.retry_bonus,
            resource_summary=resource_summary,
            overlap_risk_summary={"count": 0, "overlaps": []},
            score_summary=scheduler_score.score_summary,
        )
        operation_payload: dict[str, Any] = {
            "action": PR_ADOPTION_OPERATION_ACTION,
            "repo_slug": repo.slug(),
            "pr_number": metadata.number,
            "pr_url": metadata.url,
            "auto_merge": request.auto_merge,
            "execution": _requested_execution_policy(request),
            "reason": operator_reason,
            "logical_idempotency_key": logical_idempotency_key,
            "workspace_idempotency_key": idempotency_key,
            "previous_terminal_adoptions": previous_terminal_adoptions,
        }
        if superseded_payload is not None:
            operation_payload["superseded_adoption"] = superseded_payload
        operation = await OperationRepository(self._session).create(
            workspace_id=workspace.id,
            operation_type=OperationType.adopt_pr,
            status=OperationStatus.succeeded,
            idempotency_key=idempotency_key,
            payload=operation_payload,
        )
        event_payload: dict[str, Any] = {
            "operation_id": operation.id,
            "repo_slug": repo.slug(),
            "pr_number": metadata.number,
            "pr_url": metadata.url,
            "head_ref": metadata.head_ref,
            "head_repo_slug": metadata.head_repo_slug,
            "head_repo_url": _github_repo_url_like(repo_url, metadata.head_repo_slug),
            "base_ref": metadata.base_ref,
            "head_sha": metadata.head_sha,
            "base_sha": metadata.base_sha,
            "auto_merge": request.auto_merge,
            "execution": _requested_execution_policy(request),
            "reason": operator_reason,
            "logical_idempotency_key": logical_idempotency_key,
            "workspace_idempotency_key": idempotency_key,
            "previous_terminal_adoptions": previous_terminal_adoptions,
        }
        if superseded_payload is not None:
            event_payload["superseded_adoption"] = superseded_payload
        await workspace_repo.add_event(
            workspace,
            event_type=PR_ADOPTION_REQUESTED_EVENT_TYPE,
            reason_code=PR_ADOPTION_REQUESTED_REASON,
            payload=event_payload,
        )
        from awf.service.agent_deprecation import emit_agent_deprecated_event  # noqa: E402

        await emit_agent_deprecated_event(
            workspace_repo,
            workspace,
            agent=request.agent,
            selection_path="adopt_pr",
        )
        if superseded_workspace is not None and superseded_payload is not None:
            await workspace_repo.add_event(
                superseded_workspace,
                event_type=PR_ADOPTION_SUPERSEDED_EVENT_TYPE,
                reason_code=PR_ADOPTION_SUPERSEDED_REASON,
                payload=superseded_payload,
            )
            _log.info(
                "pr_monitor_adoption.superseded_terminal_workspace",
                **superseded_payload,
            )
        await self._session.flush()
        return workspace

    async def _response(
        self,
        workspace: Workspace,
        *,
        attached_existing: bool,
    ) -> PullRequestMonitorAdoptionResponse:
        attempt = await TaskAttemptRepository(self._session).get_by_workspace_id(workspace.id)
        candidate: MergeCandidate | None = None
        if attempt is not None:
            candidate = await MergeCandidateRepository(self._session).get_by_attempt_id(attempt.id)
        validation_runs = await ValidationRunRepository(self._session).list_for_workspace(
            workspace.id
        )
        adoption = _adoption_policy(workspace)
        repo_slug = str(adoption.get("repo_slug") or RepoRef.from_url(workspace.repo_url).slug())
        pr_number = int(adoption.get("pr_number") or workspace.pr_number or 0)
        pr_url = str(adoption.get("pr_url") or workspace.pr_url or "")
        # The persisted ``auto_merge`` column is only authoritative once the
        # provisioner has resolved it against the materialized profile; before then
        # it is the provisional seed, which lies for an adoption that omitted an
        # explicit intent and whose (trusted) profile resolves ``monitor.auto_merge``
        # on. ``auto_merge_is_resolved`` owns that rule (shared with the workspace
        # GET/list projection), so report ``None`` rather than a false ``manual``
        # policy while the setting is still unresolved.
        auto_merge_intent = auto_merge_intent_from_policy(workspace.task_policy)
        auto_merge_resolved = auto_merge_is_resolved(workspace.status, workspace.task_policy)
        auto_merge_value: bool | None = workspace.auto_merge if auto_merge_resolved else None
        return PullRequestMonitorAdoptionResponse(
            workspace_id=workspace.id,
            status=_workspace_status_for_response(workspace.status),
            version=workspace.version,
            task_id=attempt.task_id if attempt is not None else None,
            attempt_id=attempt.id if attempt is not None else None,
            candidate_id=candidate.id if candidate is not None else None,
            repo_slug=repo_slug,
            repo_url=workspace.repo_url,
            pr_number=pr_number,
            pr_url=pr_url,
            head_ref=str(adoption.get("head_ref") or workspace.remote_push_branch or ""),
            base_ref=str(adoption.get("base_ref") or workspace.branch_base),
            head_sha=_optional_str(adoption.get("head_sha")) or workspace.monitor_last_commit_sha,
            base_sha=_optional_str(adoption.get("base_sha")),
            auto_merge=auto_merge_value,
            monitor_policy={
                "auto_merge": auto_merge_value,
                "auto_merge_intent": auto_merge_intent,
                "auto_merge_resolved": auto_merge_resolved,
                "initial_review_grace_period_seconds": (
                    workspace.initial_review_grace_period_seconds
                ),
                "execution": pr_adoption_execution_policy(workspace.task_policy),
            },
            attached_existing=attached_existing,
            validation_provenance=validation_freshness_summary(
                workspace,
                validation_runs,
                candidate=candidate,
            ),
            status_url=f"/v1/workspaces/{workspace.id}",
            events_url=f"/v1/workspaces/{workspace.id}/events",
            logs_url=f"/v1/workspaces/{workspace.id}/logs",
        )


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
        defaults = defaults_with_model_overrides({request.agent: request.model})
        agent_defaults = defaults.get(request.agent)
        if agent_defaults is not None and agent_defaults.effort is not None:
            policy["agent_effort"] = agent_defaults.effort
    return policy


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


def _superseded_adoption_idempotency_key(*, idempotency_key: str, workspace_id: str) -> str:
    return f"{idempotency_key}:superseded:{workspace_id}"


def _superseded_adoption_external_id(*, external_id: str, workspace_id: str) -> str:
    return f"{external_id}:superseded:{workspace_id}"


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
