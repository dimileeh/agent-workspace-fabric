"""First-class adoption of existing GitHub PRs into AWF monitoring."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import (
    PullRequestMonitorAdoptionRequest,
    PullRequestMonitorAdoptionResponse,
)
from awf.common.audit import redact_audit_text
from awf.common.commands import AsyncioSubprocessRunner
from awf.common.config import Settings, get_settings
from awf.common.github_client import (
    PullRequestAdoptionMetadata,
    PullRequestMetadataError,
    RepoRef,
    fetch_pull_request_adoption_metadata,
    parse_github_pull_request_url,
)
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import MergeCandidate, Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.service.scheduler import scheduler_score_from_workspace
from awf.service.validation_observability import validation_freshness_summary

PR_ADOPTION_REQUESTED_EVENT_TYPE = "workspace.pr_monitor_adoption_requested"
PR_ADOPTION_REQUESTED_REASON = "PR_MONITOR_ADOPTION_REQUESTED"
PR_ADOPTION_ADMITTED_REASON = "PR_ADOPTION_ADMITTED"
PR_ADOPTION_OPERATION_ACTION = "adopt_pr_monitor"
PR_ADOPTION_TASK_KIND = "sync_feature_pr"
PR_ADOPTION_SUPERSEDED_EVENT_TYPE = "workspace.pr_monitor_adoption_superseded"
PR_ADOPTION_SUPERSEDED_REASON = "PR_MONITOR_ADOPTION_SUPERSEDED"
_FRESH_ADOPTION_ALLOWED_STATUSES = frozenset(
    {
        WorkspaceStatus.cancelled.value,
        WorkspaceStatus.completed.value,
        WorkspaceStatus.destroyed.value,
        WorkspaceStatus.failed.value,
    }
)
# Keep the public adoption error-code contract present in service source so
# docs parity tests can cross-reference the matrix against implementation.
_PR_ADOPTION_ERROR_CODE_CONTRACT = (
    {"error_code": "PR_ADOPTION_INPUT_REQUIRED"},
    {"error_code": "INVALID_GITHUB_REPO"},
    {"error_code": "PR_NOT_FOUND"},
    {"error_code": "PR_ALREADY_CLOSED"},
    {"error_code": "PR_ALREADY_MERGED"},
    {"error_code": "PR_METADATA_FETCH_FAILED"},
    {"error_code": "PR_METADATA_INVALID"},
    {"error_code": "PR_ADOPTION_POLICY_CONFLICT"},
)

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
        idempotency_key = pr_adoption_idempotency_key(
            repo_slug=repo.slug(),
            pr_number=pr_number,
        )
        workspace_repo = WorkspaceRepository(self._session)
        await workspace_repo.acquire_idempotency_key_lock(idempotency_key)
        # Re-read under the transaction lock so a concurrent adopter that won
        # the race attaches here instead of surfacing the unique constraint.
        existing = await workspace_repo.get_by_idempotency_key(idempotency_key)
        fresh_task_identity = False
        if existing is not None:
            if not _allows_fresh_adoption(existing):
                _raise_if_policy_conflicts(existing, request, repo=repo)
                return await self._response(existing, attached_existing=True)

            fresh_task_identity = True
            metadata = await self._fetch_metadata(repo=repo, pr_number=pr_number)
            await self._archive_terminal_adoption_key(
                workspace_repo=workspace_repo,
                workspace=existing,
                idempotency_key=idempotency_key,
                repo=repo,
                pr_number=pr_number,
            )
        else:
            metadata = await self._fetch_metadata(repo=repo, pr_number=pr_number)

        workspace = await self._create_adoption_workspace(
            request=request,
            repo=repo,
            metadata=metadata,
            idempotency_key=idempotency_key,
            fresh_task_identity=fresh_task_identity,
        )
        return await self._response(workspace, attached_existing=False)

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

    async def _archive_terminal_adoption_key(
        self,
        *,
        workspace_repo: WorkspaceRepository,
        workspace: Workspace,
        idempotency_key: str,
        repo: RepoRef,
        pr_number: int,
    ) -> None:
        previous_key = workspace.idempotency_key
        workspace.idempotency_key = _archived_adoption_idempotency_key(
            idempotency_key=idempotency_key,
            workspace_id=workspace.id,
        )
        await workspace_repo.add_event(
            workspace,
            event_type=PR_ADOPTION_SUPERSEDED_EVENT_TYPE,
            reason_code=PR_ADOPTION_SUPERSEDED_REASON,
            payload={
                "repo_slug": repo.slug(),
                "pr_number": pr_number,
                "previous_workspace_id": workspace.id,
                "previous_status": workspace.status,
                "previous_idempotency_key": previous_key,
                "new_idempotency_key": workspace.idempotency_key,
            },
        )
        await self._session.flush()

    async def _create_adoption_workspace(
        self,
        *,
        request: PullRequestMonitorAdoptionRequest,
        repo: RepoRef,
        metadata: PullRequestAdoptionMetadata,
        idempotency_key: str,
        fresh_task_identity: bool = False,
    ) -> Workspace:
        requested_profile = _requested_inline_profile_policy(request)
        repo_url = _adoption_repo_url(request=request, repo=repo)
        task_policy = _adoption_task_policy(
            repo=repo,
            metadata=metadata,
            request=request,
            repo_url=repo_url,
        )
        operator_reason = _redacted_optional_text(request.reason)
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
            task_external_id=_adoption_external_id(
                repo_slug=repo.slug(), pr_number=metadata.number
            ),
            agent=request.agent.value,
            test_commands=[],
            requires_database=False,
            task_policy=task_policy,
            auto_merge=request.auto_merge,
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
        task_idempotency_key = idempotency_key
        if fresh_task_identity and workspace.task_external_id is not None:
            workspace.task_external_id = _fresh_adoption_task_external_id(
                external_id=workspace.task_external_id,
                workspace_id=workspace.id,
            )
            task_idempotency_key = _fresh_adoption_task_idempotency_key(
                idempotency_key=idempotency_key,
                workspace_id=workspace.id,
            )

        task = await TaskRepository(self._session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=task_idempotency_key,
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        attempt = await TaskAttemptRepository(self._session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        await ResourceReservationRepository(self._session).create(
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            node_id=self._settings.worker_node_id or "local",
            steady_cpu=self._settings.workspace_steady_cpu,
            steady_memory_gb=self._settings.workspace_steady_memory_gb,
            peak_cpu=self._settings.workspace_peak_cpu,
            peak_memory_gb=self._settings.workspace_peak_memory_gb,
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
            resource_summary={
                "node_id": self._settings.worker_node_id or "local",
                "steady_cpu": self._settings.workspace_steady_cpu,
                "steady_memory_gb": self._settings.workspace_steady_memory_gb,
                "peak_cpu": self._settings.workspace_peak_cpu,
                "peak_memory_gb": self._settings.workspace_peak_memory_gb,
                "disk_mb": None,
                "dind_slots": 0,
                "phase": "workspace_lifecycle",
                "dind_mode": "none",
            },
            overlap_risk_summary={"count": 0, "overlaps": []},
            score_summary=scheduler_score.score_summary,
        )
        operation = await OperationRepository(self._session).create(
            workspace_id=workspace.id,
            operation_type=OperationType.adopt_pr,
            status=OperationStatus.succeeded,
            idempotency_key=idempotency_key,
            payload={
                "action": PR_ADOPTION_OPERATION_ACTION,
                "repo_slug": repo.slug(),
                "pr_number": metadata.number,
                "pr_url": metadata.url,
                "auto_merge": request.auto_merge,
                "reason": operator_reason,
            },
        )
        await workspace_repo.add_event(
            workspace,
            event_type=PR_ADOPTION_REQUESTED_EVENT_TYPE,
            reason_code=PR_ADOPTION_REQUESTED_REASON,
            payload={
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
                "reason": operator_reason,
            },
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
        return PullRequestMonitorAdoptionResponse(
            workspace_id=workspace.id,
            status=WorkspaceStatus(workspace.status),
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
            auto_merge=workspace.auto_merge,
            monitor_policy={
                "auto_merge": workspace.auto_merge,
                "initial_review_grace_period_seconds": (
                    workspace.initial_review_grace_period_seconds
                ),
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
    return await fetch_pull_request_adoption_metadata(
        runner=AsyncioSubprocessRunner(),
        repo=repo,
        pr_number=pr_number,
    )


def pr_adoption_idempotency_key(*, repo_slug: str, pr_number: int) -> str:
    digest = hashlib.sha256(f"{repo_slug.lower()}#{pr_number}".encode()).hexdigest()
    return f"pr-adopt:{digest[:48]}"


def _allows_fresh_adoption(workspace: Workspace) -> bool:
    return workspace.status in _FRESH_ADOPTION_ALLOWED_STATUSES


def _archived_adoption_idempotency_key(*, idempotency_key: str, workspace_id: str) -> str:
    return f"{idempotency_key}:terminal:{workspace_id}"


def _normalize_request_identity(
    request: PullRequestMonitorAdoptionRequest,
) -> tuple[RepoRef, int]:
    if request.pr_url:
        try:
            repo, pr_number = parse_github_pull_request_url(request.pr_url)
        except ValueError as exc:
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
        _raise_if_repo_identity_conflicts(canonical_repo=repo, request=request)
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
    return repo, request.pr_number


def _raise_if_repo_identity_conflicts(
    *,
    canonical_repo: RepoRef,
    request: PullRequestMonitorAdoptionRequest,
) -> None:
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
        if requested_repo.slug().lower() != canonical_repo.slug().lower():
            raise PRMonitorAdoptionError(
                error_code="PR_ADOPTION_INPUT_REQUIRED",
                message="PR adoption repository identities refer to different repositories.",
                status_code=422,
                detail={
                    "expected_repo_slug": canonical_repo.slug(),
                    "actual_repo_slug": requested_repo.slug(),
                    "field": field_name,
                },
            )


def _adoption_task_policy(
    *,
    repo: RepoRef,
    metadata: PullRequestAdoptionMetadata,
    request: PullRequestMonitorAdoptionRequest,
    repo_url: str,
) -> dict[str, Any]:
    return {
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
        },
    }


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


def _fresh_adoption_task_external_id(*, external_id: str, workspace_id: str) -> str:
    return f"{external_id}:{workspace_id}"


def _fresh_adoption_task_idempotency_key(*, idempotency_key: str, workspace_id: str) -> str:
    return f"{idempotency_key}:task:{workspace_id}"


def _adoption_repo_url(*, request: PullRequestMonitorAdoptionRequest, repo: RepoRef) -> str:
    return request.repo_url or repo.ssh_url()


def _github_repo_url_like(repo_url: str, repo_slug: str) -> str:
    return RepoRef.from_url(repo_slug).clone_url_like(repo_url)


def _raise_if_policy_conflicts(
    workspace: Workspace,
    request: PullRequestMonitorAdoptionRequest,
    *,
    repo: RepoRef,
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
    if workspace.requested_profile != requested_profile:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different inline profile policy.",
            detail={
                "workspace_id": workspace.id,
                "existing_inline_profile_name": _inline_profile_name(workspace.requested_profile),
                "requested_inline_profile_name": _inline_profile_name(requested_profile),
            },
        )
    if workspace.auto_merge != request.auto_merge:
        raise PRMonitorAdoptionError(
            error_code="PR_ADOPTION_POLICY_CONFLICT",
            message="Existing adopted PR monitor uses a different auto_merge policy.",
            detail={
                "workspace_id": workspace.id,
                "existing_auto_merge": workspace.auto_merge,
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
