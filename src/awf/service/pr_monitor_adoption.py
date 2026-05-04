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
# Keep the public adoption error-code contract present in service source so
# docs parity tests can cross-reference the matrix against implementation.
_PR_ADOPTION_ERROR_CODE_CONTRACT = (
    {"error_code": "PR_ADOPTION_INPUT_REQUIRED"},
    {"error_code": "INVALID_GITHUB_REPO"},
    {"error_code": "PR_NOT_FOUND"},
    {"error_code": "PR_ALREADY_CLOSED"},
    {"error_code": "PR_ALREADY_MERGED"},
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
        existing = await workspace_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            _raise_if_policy_conflicts(existing, request)
            return await self._response(existing, attached_existing=True)

        metadata = await self._fetch_metadata(repo=repo, pr_number=pr_number)
        workspace = await self._create_adoption_workspace(
            request=request,
            repo=repo,
            metadata=metadata,
            idempotency_key=idempotency_key,
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

    async def _create_adoption_workspace(
        self,
        *,
        request: PullRequestMonitorAdoptionRequest,
        repo: RepoRef,
        metadata: PullRequestAdoptionMetadata,
        idempotency_key: str,
    ) -> Workspace:
        requested_profile = (
            request.profile.model_dump(mode="json", by_alias=True)
            if request.profile is not None
            else None
        )
        task_policy = _adoption_task_policy(
            repo=repo,
            metadata=metadata,
            request=request,
        )
        operator_reason = _redacted_optional_text(request.reason)
        workspace_repo = WorkspaceRepository(self._session)
        workspace = await workspace_repo.create(
            repo_url=request.repo_url or repo.https_url(),
            branch_base=metadata.base_ref,
            task_title=(
                request.task_title
                or f"PR monitor adoption: {repo.slug()}#{metadata.number}"
            ),
            task_prompt=request.task_prompt
            or _adoption_task_prompt(repo=repo, metadata=metadata),
            task_external_id=_adoption_external_id(repo_slug=repo.slug(), pr_number=metadata.number),
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
        workspace.monitor_last_commit_sha = metadata.head_sha

        task = await TaskRepository(self._session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=idempotency_key,
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
    return repo, request.pr_number


def _adoption_task_policy(
    *,
    repo: RepoRef,
    metadata: PullRequestAdoptionMetadata,
    request: PullRequestMonitorAdoptionRequest,
) -> dict[str, Any]:
    return {
        "task_kind": PR_ADOPTION_TASK_KIND,
        "pr_adoption": {
            "repo_slug": repo.slug(),
            "pr_number": metadata.number,
            "pr_url": metadata.url,
            "head_ref": metadata.head_ref,
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


def _raise_if_policy_conflicts(
    workspace: Workspace,
    request: PullRequestMonitorAdoptionRequest,
) -> None:
    requested_grace = request.initial_review_grace_period_seconds
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
            message=(
                "Existing adopted PR monitor uses a different initial review "
                "grace policy."
            ),
            detail={
                "workspace_id": workspace.id,
                "existing_initial_review_grace_period_seconds": (
                    workspace.initial_review_grace_period_seconds
                ),
                "requested_initial_review_grace_period_seconds": requested_grace,
            },
        )


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
