"""Preserved active execution recovery queries.

Mechanically extracted from recovery_preserved.py; behavior is unchanged.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from inspect import (
    getattr_static,
)
from pathlib import Path
from typing import Any, cast

from sqlalchemy import (
    and_,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.forge import (
    OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED_REASON_CODE,
    concrete_forge_for_repo,
)
from awf.common.git_identity import (
    git_safe_directory_config_args,
)
from awf.control.worker.constants import (
    _ACTIVE_EXECUTION_SALVAGE_BLOCKED_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_SOURCE,
    _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
    _ACTIVE_EXECUTION_STATUSES,
    _PRESERVED_ACTIVE_GIT_TIMEOUT_SECONDS,
)
from awf.control.worker.helpers import (
    _expected_open_pr_head_repo_slug,
    _open_pull_request_summary,
    _utc_datetime,
)
from awf.control.worker.logging import _log
from awf.control.worker.status_values import (
    _preserved_active_execution_status_values,
    _salvage_workspace_status_values,
)
from awf.control.worker.types import (
    _ActiveExecutionCandidate,
    _BranchOpenPRLookup,
    _PreservedWorktreeClassification,
)
from awf.db.enums import (
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.models import (
    Operation,
    Workspace,
    WorkspaceEvent,
)
from awf.db.repositories import WorkspaceRepository
from awf.service.workspace_runtime_health import (
    ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
    ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
    OPERATOR_REFRESH_EVENT_TYPE,
    OPERATOR_REFRESH_REASON_CODE,
)


async def _has_operator_refresh_after_latest_preservation(
    self: Any,
    candidate: _ActiveExecutionCandidate,
) -> bool:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return False
        return cast(
            bool,
            await self._has_operator_refresh_after_latest_preservation_for_workspace(
                session,
                ws,
                candidate.status,
            ),
        )


async def _has_operator_refresh_after_latest_preservation_for_workspace(
    self: Any,
    session: AsyncSession,
    workspace: Workspace,
    status: WorkspaceStatus,
) -> bool:
    event_floor = await self._active_execution_preservation_event_floor(
        session,
        workspace,
        status,
        include_operator_refresh=False,
        include_execution_claim_expiry=False,
    )
    latest_refresh = await self._latest_operator_refresh_requested_at(
        session,
        workspace.id,
        event_floor=event_floor,
    )
    if latest_refresh is None:
        return False
    latest_preservation = await self._latest_preserved_active_execution_at(
        session,
        workspace.id,
        status,
        event_floor=event_floor,
    )
    if latest_preservation is None:
        return False
    return _utc_datetime(latest_refresh) >= _utc_datetime(latest_preservation)


async def _has_current_preserved_active_execution(
    self: Any,
    candidate: _ActiveExecutionCandidate,
) -> bool:
    if candidate.status not in _ACTIVE_EXECUTION_STATUSES:
        return False

    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return False
        event_floor = await self._active_execution_preservation_event_floor(
            session,
            ws,
            candidate.status,
        )
        latest_preserved = await self._latest_preserved_active_execution_at(
            session,
            candidate.workspace_id,
            candidate.status,
            event_floor=event_floor,
        )
        if latest_preserved is None:
            return False
        return not self._active_execution_preservation_is_expired(latest_preserved)


async def _has_expired_preserved_active_execution(
    self: Any,
    candidate: _ActiveExecutionCandidate,
) -> bool:
    if candidate.status not in _ACTIVE_EXECUTION_STATUSES:
        return False

    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return False
        event_floor = await self._active_execution_preservation_event_floor(
            session,
            ws,
            candidate.status,
        )
        latest_preserved = await self._latest_preserved_active_execution_at(
            session,
            candidate.workspace_id,
            candidate.status,
            event_floor=event_floor,
        )
        if latest_preserved is None:
            return False
        return cast(bool, self._active_execution_preservation_is_expired(latest_preserved))


def _active_execution_preservation_is_expired(self: Any, preserved_at: datetime) -> bool:
    grace = max(0.0, self._config.active_execution_preservation_grace_seconds)
    return datetime.now(UTC) - _utc_datetime(preserved_at) >= timedelta(seconds=grace)


async def _has_preserved_active_execution_event(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
    status: WorkspaceStatus,
    *,
    event_floor: datetime | None = None,
) -> bool:
    _ = self
    stmt = (
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
            WorkspaceEvent.reason_code == ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
            WorkspaceEvent.payload["workspace_status"].as_string() == status.value,
        )
        .limit(1)
    )
    if event_floor is not None:
        stmt = stmt.where(WorkspaceEvent.occurred_at >= event_floor)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _latest_preserved_active_execution_at(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
    status: WorkspaceStatus,
    *,
    event_floor: datetime | None = None,
    match_active_execution_statuses: bool = False,
) -> datetime | None:
    _ = self
    status_values = _preserved_active_execution_status_values(
        status,
        match_active_execution_statuses=match_active_execution_statuses,
    )
    stmt = (
        select(WorkspaceEvent.occurred_at)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
            WorkspaceEvent.reason_code == ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
            WorkspaceEvent.payload["workspace_status"].as_string().in_(status_values),
        )
        .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
        .limit(1)
    )
    if event_floor is not None:
        stmt = stmt.where(WorkspaceEvent.occurred_at >= event_floor)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _latest_preserved_active_execution_event(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
    status: WorkspaceStatus,
    *,
    event_floor: datetime | None = None,
    match_active_execution_statuses: bool = False,
) -> WorkspaceEvent | None:
    _ = self
    status_values = _preserved_active_execution_status_values(
        status,
        match_active_execution_statuses=match_active_execution_statuses,
    )
    stmt = (
        select(WorkspaceEvent)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
            WorkspaceEvent.reason_code == ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
            WorkspaceEvent.payload["workspace_status"].as_string().in_(status_values),
        )
        .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
        .limit(1)
    )
    if event_floor is not None:
        stmt = stmt.where(WorkspaceEvent.occurred_at >= event_floor)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _has_current_salvage_event(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
    *,
    event_type: str,
    reason_code: str,
    event_floor: datetime,
    workspace_status: WorkspaceStatus,
) -> bool:
    _ = self
    workspace_status_values = _salvage_workspace_status_values(
        workspace_status,
        event_type=event_type,
        reason_code=reason_code,
    )
    stmt = (
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == event_type,
            WorkspaceEvent.reason_code == reason_code,
            WorkspaceEvent.occurred_at >= event_floor,
            WorkspaceEvent.payload["workspace_status"].as_string().in_(workspace_status_values),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _latest_current_salvage_blocked_reason(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
    *,
    event_floor: datetime,
    workspace_status: WorkspaceStatus,
) -> str | None:
    _ = self
    workspace_status_values = _salvage_workspace_status_values(
        workspace_status,
        event_type=_ACTIVE_EXECUTION_SALVAGE_BLOCKED_EVENT_TYPE,
        reason_code=_ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE,
    )
    blocked_reason = WorkspaceEvent.payload["blocked_reason"].as_string()
    stmt = (
        select(blocked_reason)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == _ACTIVE_EXECUTION_SALVAGE_BLOCKED_EVENT_TYPE,
            WorkspaceEvent.reason_code == _ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE,
            WorkspaceEvent.occurred_at >= event_floor,
            WorkspaceEvent.payload["workspace_status"].as_string().in_(workspace_status_values),
        )
        .order_by(
            WorkspaceEvent.event_order.desc().nullslast(),
            WorkspaceEvent.occurred_at.desc(),
            WorkspaceEvent.id.desc(),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _has_active_preserved_validation_recovery(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
    *,
    preservation_event_id: str | None = None,
) -> bool:
    _ = self
    recovery_mode = Operation.payload["recovery_mode"].as_string()
    stmt = (
        select(literal(1))
        .select_from(Operation)
        .where(
            Operation.workspace_id == workspace_id,
            Operation.status.in_(
                (
                    OperationStatus.pending.value,
                    OperationStatus.running.value,
                )
            ),
            Operation.payload["source"].as_string() == _ACTIVE_EXECUTION_SALVAGE_SOURCE,
            or_(
                and_(
                    Operation.type == OperationType.validate.value,
                    recovery_mode.in_(("validate_only", "rebase_only")),
                ),
                and_(
                    Operation.type == OperationType.rebase.value,
                    recovery_mode == "rebase_only",
                ),
            ),
            Operation.payload["reason_code"].as_string()
            == _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
        )
    )
    if preservation_event_id is not None:
        stmt = stmt.where(
            or_(
                Operation.payload["preservation_event_id"].as_string() == preservation_event_id,
                Operation.payload["preservation_event"]["id"].as_string() == preservation_event_id,
            )
        )
    exists_stmt = stmt.limit(1).exists()
    return bool((await session.execute(select(exists_stmt))).scalar_one())


async def _latest_operator_refresh_requested_at(
    self: Any,
    session: AsyncSession,
    workspace_id: str,
    *,
    event_floor: datetime | None = None,
) -> datetime | None:
    _ = self
    stmt = (
        select(WorkspaceEvent.occurred_at)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == OPERATOR_REFRESH_EVENT_TYPE,
            WorkspaceEvent.reason_code == OPERATOR_REFRESH_REASON_CODE,
        )
        .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
        .limit(1)
    )
    if event_floor is not None:
        stmt = stmt.where(WorkspaceEvent.occurred_at >= event_floor)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _active_execution_preservation_event_floor(
    self: Any,
    session: AsyncSession,
    workspace: Workspace,
    status: WorkspaceStatus,
    *,
    include_operator_refresh: bool = True,
    include_execution_claim_expiry: bool = True,
) -> datetime | None:
    floors: list[datetime] = []
    if include_execution_claim_expiry and workspace.execution_claim_expires_at is not None:
        claim_floor = _utc_datetime(workspace.execution_claim_expires_at)
        if claim_floor <= datetime.now(UTC):
            floors.append(claim_floor)

    stmt = (
        select(WorkspaceEvent.occurred_at)
        .where(
            WorkspaceEvent.workspace_id == workspace.id,
            WorkspaceEvent.event_type == "workspace.state_changed",
            WorkspaceEvent.new_state == status.value,
        )
        .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
        .limit(1)
    )
    status_started_at = (await session.execute(stmt)).scalar_one_or_none()
    if status_started_at is not None:
        floors.append(_utc_datetime(status_started_at))

    # An operator refresh is an explicit recovery request for preserved runtime.
    # Preservation evidence older than that request must not keep scans skipped.
    if include_operator_refresh:
        refresh_requested_at = await self._latest_operator_refresh_requested_at(
            session,
            workspace.id,
        )
        if refresh_requested_at is not None:
            floors.append(_utc_datetime(refresh_requested_at))

    return max(floors) if floors else None


async def _active_execution_preservation_salvage_event_floor(
    self: Any,
    session: AsyncSession,
    workspace: Workspace,
    status: WorkspaceStatus,
) -> datetime | None:
    if status not in _ACTIVE_EXECUTION_STATUSES:
        return cast(
            datetime | None,
            await self._active_execution_preservation_event_floor(
                session,
                workspace,
                status,
            ),
        )

    floors: list[datetime] = []
    active_status_values = tuple(status.value for status in _ACTIVE_EXECUTION_STATUSES)
    inactive_state_stmt = (
        select(WorkspaceEvent.occurred_at)
        .where(
            WorkspaceEvent.workspace_id == workspace.id,
            WorkspaceEvent.event_type == "workspace.state_changed",
            ~WorkspaceEvent.new_state.in_(active_status_values),
        )
        .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
        .limit(1)
    )
    inactive_state_at = (await session.execute(inactive_state_stmt)).scalar_one_or_none()
    if inactive_state_at is not None:
        floors.append(_utc_datetime(inactive_state_at))

    refresh_requested_at = await self._latest_operator_refresh_requested_at(
        session,
        workspace.id,
    )
    if refresh_requested_at is not None:
        floors.append(_utc_datetime(refresh_requested_at))

    return max(floors) if floors else None


async def _resolve_preserved_active_branch_open_pr(
    self: Any,
    *,
    repo_url: str | None,
    branch_name: str | None,
    base_branch: str | None,
    expected_head_repo_slug: str | None = None,
    resolved_forge: object = None,
) -> _BranchOpenPRLookup | None:
    if self._open_pr_resolver is None:
        return None
    if repo_url is None or not repo_url.strip():
        return None
    if branch_name is None or not branch_name.strip():
        return None

    lookup_branch = branch_name.strip()

    # Gate any non-GitHub forge BEFORE the GitHub-only ``gh pr list`` path.
    # ``BranchOpenPullRequestResolver`` parses ``repo_url`` into a ``RepoRef`` and
    # shells ``gh pr list --repo owner/repo`` (GitHub), dropping the forge host —
    # so a preserved-active BitBucket workspace would otherwise be queried as a
    # *same-slug GitHub* repo and could attach the wrong monitor instead of
    # failing fast. The executor forge gate does not cover this worker recovery
    # path, so reject here.
    #
    # Resolve the gate forge via ``concrete_forge_for_repo`` (persisted/resolved
    # forge > repo-URL host > github), mirroring the executor forge gate, NOT plain
    # URL detection. A bare ``owner/repo`` slug carries no host, so URL detection
    # alone resolves it as GitHub (``RepoRef.from_url`` defaults bare slugs to
    # github) — a BitBucket workspace configured with ``forge: bitbucket`` but a
    # bare-slug ``repo_url`` would then slip past this gate into the GitHub
    # resolver. Honoring the persisted forge first closes that gap.
    #
    # Issue #345 Part 2 note: this gate is GitHub-ONLY on purpose, deliberately
    # NOT the shared ``ensure_forge_supported`` set. BitBucket Cloud is now a
    # supported forge generally (Part 2 ships ``BitBucketClient``), but the
    # forge-NEUTRAL open-PR resolver is explicitly out of scope, so this still-
    # GitHub-only path must keep rejecting bitbucket (and any future forge) until
    # a forge-neutral resolver exists. Undetectable URLs (``None``) fall through
    # to the GitHub resolver unchanged — existing GitHub workspaces never trip
    # this.
    #
    # The reason code is ``OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED``, NOT the generic
    # ``FORGE_NOT_SUPPORTED``: BitBucket Cloud *is* a supported forge now, so the
    # generic code (and its "use a supported forge" fix text) would contradict the
    # real failure for a bitbucket.org operator. The honest reason is that the
    # open-PR resolver itself is GitHub-only.
    gate_forge = concrete_forge_for_repo(resolved_forge, repo_url)
    if gate_forge != "github":
        _log.warning(
            "worker.preserved_active_open_pr_lookup_forge_not_supported",
            branch_name=lookup_branch,
            base_branch=base_branch,
            forge=gate_forge,
            reason_code=OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED_REASON_CODE,
        )
        return _BranchOpenPRLookup(
            branch_name=lookup_branch,
            state="failed",
            ambiguity_reason="open_pr_lookup_forge_not_supported",
            payload={
                "branch_name": lookup_branch,
                "forge": gate_forge,
                "reason_code": OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED_REASON_CODE,
                "failure": "open_pr_resolver_forge_not_supported",
                "source": "open_pr_resolver",
            },
        )

    try:
        matches = await self._open_pr_resolver.resolve(
            repo_url=repo_url,
            branch_name=lookup_branch,
            base_branch=base_branch,
        )
    except Exception as exc:
        _log.warning(
            "worker.preserved_active_open_pr_lookup_failed",
            branch_name=lookup_branch,
            base_branch=base_branch,
            error_type=type(exc).__name__,
        )
        return _BranchOpenPRLookup(
            branch_name=lookup_branch,
            state="failed",
            ambiguity_reason="open_pr_lookup_failed",
            payload={
                "branch_name": lookup_branch,
                "error_type": type(exc).__name__,
                "failure": "resolver_exception",
                "source": "open_pr_resolver",
            },
        )

    try:
        summaries = [
            _open_pull_request_summary(match, branch_name=lookup_branch) for match in matches
        ]
    except ValueError as exc:
        return _BranchOpenPRLookup(
            branch_name=lookup_branch,
            state="failed",
            ambiguity_reason="open_pr_lookup_invalid",
            payload={
                "branch_name": lookup_branch,
                "error": str(exc),
                "source": "open_pr_resolver",
            },
        )

    explicit_expected_head_repo_slug = (
        expected_head_repo_slug.strip()
        if isinstance(expected_head_repo_slug, str) and expected_head_repo_slug.strip()
        else None
    )
    resolved_expected_head_repo_slug = explicit_expected_head_repo_slug or (
        _expected_open_pr_head_repo_slug(repo_url)
    )
    if resolved_expected_head_repo_slug is not None and summaries:
        head_repo_matches = [
            summary
            for summary in summaries
            if summary.head_repo_slug is not None
            and summary.head_repo_slug.lower() == resolved_expected_head_repo_slug.lower()
        ]
        if not head_repo_matches:
            return _BranchOpenPRLookup(
                branch_name=lookup_branch,
                state="ambiguous",
                ambiguity_reason="open_pr_head_repo_mismatch",
                payload={
                    "branch_name": lookup_branch,
                    "expected_head_repo_slug": resolved_expected_head_repo_slug,
                    "match_count": len(summaries),
                    "matches": [summary.to_payload() for summary in summaries],
                    "source": "open_pr_resolver",
                },
            )
        summaries = head_repo_matches

    if not summaries:
        return _BranchOpenPRLookup(
            branch_name=lookup_branch,
            state="none",
            payload={
                "branch_name": lookup_branch,
                "match_count": 0,
                "source": "open_pr_resolver",
            },
        )
    if len(summaries) == 1:
        payload: dict[str, Any] = {
            "branch_name": lookup_branch,
            "match_count": 1,
            "source": "open_pr_resolver",
        }
        if explicit_expected_head_repo_slug is not None:
            payload["expected_head_repo_slug"] = explicit_expected_head_repo_slug
        return _BranchOpenPRLookup(
            branch_name=lookup_branch,
            state="matched",
            match=summaries[0],
            payload=payload,
        )
    return _BranchOpenPRLookup(
        branch_name=lookup_branch,
        state="ambiguous",
        ambiguity_reason="multiple_open_prs_for_branch",
        payload={
            "branch_name": lookup_branch,
            "match_count": len(summaries),
            "matches": [summary.to_payload() for summary in summaries],
            "source": "open_pr_resolver",
        },
    )


async def _classify_preserved_active_worktree(
    self: Any,
    *,
    workspace_id: str,
    expected_branch_name: str | None,
    base_commit: str | None,
) -> _PreservedWorktreeClassification:
    worktree_path = self._preserved_active_worktree_path(workspace_id)
    if worktree_path is None:
        return _PreservedWorktreeClassification(
            state="failed",
            reason="worktree_root_unavailable",
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
        )
    if not worktree_path.exists():
        return _PreservedWorktreeClassification(
            state="no_work",
            reason="worktree_missing",
            worktree_path=str(worktree_path),
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
        )
    if base_commit is None or not base_commit.strip():
        return _PreservedWorktreeClassification(
            state="ambiguous",
            reason="missing_base_commit",
            worktree_path=str(worktree_path),
            expected_branch_name=expected_branch_name,
        )

    branch = await self._run_preserved_active_git(worktree_path, "branch", "--show-current")
    if not branch[0]:
        return _PreservedWorktreeClassification(
            state="failed",
            reason="branch_unavailable",
            worktree_path=str(worktree_path),
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
            error=branch[2],
        )
    branch_name = branch[1].strip()
    if not branch_name:
        return _PreservedWorktreeClassification(
            state="ambiguous",
            reason="detached_head",
            worktree_path=str(worktree_path),
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
        )
    if expected_branch_name is None:
        return _PreservedWorktreeClassification(
            state="ambiguous",
            reason="missing_branch_name",
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
        )
    if branch_name != expected_branch_name:
        return _PreservedWorktreeClassification(
            state="ambiguous",
            reason="branch_mismatch",
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
        )

    head = await self._run_preserved_active_git(worktree_path, "rev-parse", "HEAD")
    if not head[0]:
        return _PreservedWorktreeClassification(
            state="failed",
            reason="head_unavailable",
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
            error=head[2],
        )
    head_sha = head[1].strip()
    status = await self._run_preserved_active_git(
        worktree_path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if not status[0]:
        return _PreservedWorktreeClassification(
            state="failed",
            reason="status_unavailable",
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
            head_sha=head_sha,
            error=status[2],
        )
    status_porcelain = status[1].strip()
    if status_porcelain:
        return _PreservedWorktreeClassification(
            state="ambiguous",
            reason="dirty_worktree",
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
            head_sha=head_sha,
            status_porcelain=status_porcelain,
        )

    count = await self._run_preserved_active_git(
        worktree_path,
        "rev-list",
        "--count",
        f"{base_commit}..HEAD",
    )
    if not count[0]:
        return _PreservedWorktreeClassification(
            state="failed",
            reason="ahead_count_unavailable",
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
            head_sha=head_sha,
            error=count[2],
        )
    try:
        commit_count = int(count[1].strip())
    except ValueError:
        return _PreservedWorktreeClassification(
            state="failed",
            reason="ahead_count_invalid",
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
            head_sha=head_sha,
            error=count[1],
        )
    if commit_count > 0:
        return _PreservedWorktreeClassification(
            state="committed",
            reason="clean_branch_ahead",
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            expected_branch_name=expected_branch_name,
            base_commit=base_commit,
            head_sha=head_sha,
            commit_count=commit_count,
            status_porcelain=status_porcelain,
        )
    return _PreservedWorktreeClassification(
        state="no_work",
        reason="clean_branch_not_ahead",
        worktree_path=str(worktree_path),
        branch_name=branch_name,
        expected_branch_name=expected_branch_name,
        base_commit=base_commit,
        head_sha=head_sha,
        commit_count=commit_count,
        status_porcelain=status_porcelain,
    )


def _preserved_active_worktree_path(self: Any, workspace_id: str) -> Path | None:
    try:
        getattr_static(self._provisioner, "get_worktree_path")
    except AttributeError as exc:
        _log.warning(
            "worker.preserved_active_worktree_path_unavailable",
            workspace_id=workspace_id,
            reason_code="PRESERVED_ACTIVE_WORKTREE_ROOT_UNAVAILABLE",
            error_type=type(exc).__name__,
            error=str(exc)[:240],
        )
        return None
    worktree_path = self._provisioner.get_worktree_path(workspace_id)
    return worktree_path if isinstance(worktree_path, Path) else None


async def _run_preserved_active_git(
    self: Any,
    worktree_path: Path,
    *args: str,
) -> tuple[bool, str, str]:
    _ = self
    command = [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
        *args,
    ]

    def _timeout_output(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value

    def _run() -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=_PRESERVED_ACTIVE_GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = _timeout_output(exc.stderr).rstrip()
            git_args = " ".join(args)
            git_command = f"git {git_args}" if git_args else "git"
            timeout_message = (
                f"{git_command} timed out after {_PRESERVED_ACTIVE_GIT_TIMEOUT_SECONDS:g}s"
            )
            return subprocess.CompletedProcess(
                args=command,
                returncode=124,
                stdout=_timeout_output(exc.stdout),
                stderr=f"{stderr}\n{timeout_message}" if stderr else timeout_message,
            )

    result = await asyncio.to_thread(_run)
    return result.returncode == 0, result.stdout, result.stderr
