"""Quality and conformance database repositories."""

from __future__ import annotations

import builtins
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from awf.common.ids import (
    new_merge_candidate_id,
    new_policy_finding_id,
    new_resource_reservation_id,
    new_stale_reason_id,
    new_validation_run_id,
)
from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import (
    MergeCandidate,
    PolicyFinding,
    PRFeedbackResolution,
    ResourceReservation,
    StaleReason,
    Task,
    TaskAttempt,
    ValidationRun,
    Workspace,
)
from awf.db.repositories.base import (
    PolicyFindingCreate,
    StaleReasonCreate,
    _active_latest_resource_reservation_totals_stmt,
    _coverage_metadata_has_pytest_failures,
    _fetch_resource_reservation_totals,
    _normalize_provider_key,
    _normalize_repository_key,
    _pr_feedback_resolution_upsert_stmt,
    empty_resource_reservation_totals,
    pr_feedback_body_hash,
    resolve_session_dialect_name,
    validation_command_set_hash,
)
from awf.db.repositories.task_repo import TaskAttemptRepository
from awf.db.validation_runs import validation_run_coverage_payload
from awf.runtime.merge_eligibility import (
    DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
    VALIDATION_MISSING_FOR_CURRENT_HEAD_STALE_REASON,
)


class ResourceReservationRepository:
    """CRUD helpers for local resource reservation records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: str,
        attempt_id: str,
        node_id: str,
        steady_cpu: float,
        steady_memory_gb: float,
        peak_cpu: float,
        peak_memory_gb: float,
        disk_mb: int | None,
        dind_slots: int = 0,
        phase: str,
        reserved_at: datetime | None = None,
    ) -> ResourceReservation:
        row = ResourceReservation(
            id=new_resource_reservation_id(),
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            node_id=node_id,
            steady_cpu=steady_cpu,
            steady_memory_gb=steady_memory_gb,
            peak_cpu=peak_cpu,
            peak_memory_gb=peak_memory_gb,
            disk_mb=disk_mb,
            dind_slots=dind_slots,
            phase=phase,
            reserved_at=reserved_at or datetime.now(UTC),
            released_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_for_workspace(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
    ) -> builtins.list[ResourceReservation]:
        stmt = (
            select(ResourceReservation)
            .where(ResourceReservation.workspace_id == workspace_id)
            .order_by(ResourceReservation.reserved_at.desc(), ResourceReservation.id.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def active_for_workspace(self, workspace_id: str) -> ResourceReservation | None:
        stmt = (
            select(ResourceReservation)
            .where(
                ResourceReservation.workspace_id == workspace_id,
                ResourceReservation.released_at.is_(None),
            )
            .order_by(ResourceReservation.reserved_at.desc(), ResourceReservation.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def active_latest_by_workspace_ids(
        self,
        workspace_ids: Iterable[str],
    ) -> dict[str, ResourceReservation]:
        ids = tuple(dict.fromkeys(workspace_ids))
        if not ids:
            return {}
        ranked_reservations = (
            select(
                ResourceReservation.id.label("reservation_id"),
                func.row_number()
                .over(
                    partition_by=ResourceReservation.workspace_id,
                    order_by=(
                        ResourceReservation.reserved_at.desc(),
                        ResourceReservation.id.desc(),
                    ),
                )
                .label("reservation_rank"),
            )
            .where(
                ResourceReservation.workspace_id.in_(ids),
                ResourceReservation.released_at.is_(None),
            )
            .subquery()
        )
        stmt = (
            select(ResourceReservation)
            .join(
                ranked_reservations,
                ResourceReservation.id == ranked_reservations.c.reservation_id,
            )
            .where(ranked_reservations.c.reservation_rank == 1)
        )
        return {
            reservation.workspace_id: reservation
            for reservation in (await self._session.execute(stmt)).scalars()
        }

    async def release_active_for_workspace(
        self,
        workspace_id: str,
        *,
        released_at: datetime | None = None,
    ) -> builtins.list[ResourceReservation]:
        release_time = released_at or datetime.now(UTC)
        result = await self._session.execute(
            update(ResourceReservation)
            .where(
                ResourceReservation.workspace_id == workspace_id,
                ResourceReservation.released_at.is_(None),
            )
            .values(released_at=release_time)
            .returning(ResourceReservation)
        )
        rows = list(result.scalars())
        rows.sort(key=lambda row: (row.reserved_at, row.id))
        return rows

    async def active_latest_totals(
        self,
        *,
        statuses: Iterable[WorkspaceStatus | str] | None = None,
        node_id: str | None = None,
    ) -> dict[str, float | int]:
        stmt = _active_latest_resource_reservation_totals_stmt(
            statuses=statuses,
            reservation_node_id=node_id,
        )
        if stmt is None:
            return empty_resource_reservation_totals()
        return await _fetch_resource_reservation_totals(self._session, stmt)

    async def active_latest_totals_for_workspace_scope(
        self,
        *,
        statuses: Iterable[WorkspaceStatus | str] | None = None,
        node_id: str | None = None,
    ) -> dict[str, float | int]:
        stmt = _active_latest_resource_reservation_totals_stmt(
            statuses=statuses,
            workspace_node_id=node_id,
        )
        if stmt is None:
            return empty_resource_reservation_totals()
        return await _fetch_resource_reservation_totals(self._session, stmt)

    async def active_latest_totals_for_scheduler_allocation_scope(
        self,
        *,
        statuses: Iterable[WorkspaceStatus | str],
        node_id: str,
    ) -> dict[str, float | int]:
        stmt = _active_latest_resource_reservation_totals_stmt(
            statuses=statuses,
            scheduler_allocation_node_id=node_id,
        )
        if stmt is None:
            return empty_resource_reservation_totals()
        return await _fetch_resource_reservation_totals(self._session, stmt)

    async def active_latest_totals_for_metrics_allocation_scope(
        self,
        *,
        statuses: Iterable[WorkspaceStatus | str],
        node_id: str,
    ) -> dict[str, float | int]:
        stmt = _active_latest_resource_reservation_totals_stmt(
            statuses=statuses,
            metrics_allocation_node_id=node_id,
        )
        if stmt is None:
            return empty_resource_reservation_totals()
        return await _fetch_resource_reservation_totals(self._session, stmt)


class MergeCandidateRepository:
    """CRUD helpers for explicit PR-backed merge candidates."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_attempt_id(self, attempt_id: str) -> MergeCandidate | None:
        stmt = (
            select(MergeCandidate)
            .where(MergeCandidate.attempt_id == attempt_id)
            .options(
                selectinload(MergeCandidate.attempt),
                selectinload(MergeCandidate.workspace),
                selectinload(MergeCandidate.task),
            )
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_open_for_workspace_with_merge_inputs(
        self,
        workspace_id: str,
    ) -> MergeCandidate | None:
        stmt = (
            select(MergeCandidate)
            .where(
                MergeCandidate.workspace_id == workspace_id,
                MergeCandidate.status == "open",
            )
            .options(
                selectinload(MergeCandidate.attempt),
                selectinload(MergeCandidate.task),
                selectinload(MergeCandidate.policy_findings),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.validation_runs),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.policy_findings),
            )
            .order_by(MergeCandidate.created_at.desc(), MergeCandidate.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_latest_for_workspace_with_merge_inputs(
        self,
        workspace_id: str,
    ) -> MergeCandidate | None:
        stmt = (
            select(MergeCandidate)
            .where(MergeCandidate.workspace_id == workspace_id)
            .options(
                selectinload(MergeCandidate.attempt),
                selectinload(MergeCandidate.task),
                selectinload(MergeCandidate.policy_findings),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.validation_runs),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.policy_findings),
            )
            .order_by(
                MergeCandidate.updated_at.desc(),
                MergeCandidate.created_at.desc(),
                MergeCandidate.id.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_task(
        self,
        task_id: str,
        *,
        limit: int = 100,
    ) -> builtins.list[MergeCandidate]:
        stmt = (
            select(MergeCandidate)
            .where(MergeCandidate.task_id == task_id)
            .options(
                selectinload(MergeCandidate.attempt),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.events),
                selectinload(MergeCandidate.task),
            )
            .order_by(
                MergeCandidate.updated_at.desc(),
                MergeCandidate.id.desc(),
            )
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_queue(
        self,
        *,
        repo_url: str | None = None,
        base_branch: str | None = None,
        status: WorkspaceStatus | str | None = None,
        before_updated_at: datetime | None = None,
        before_workspace_id: str | None = None,
        limit: int = 50,
    ) -> builtins.list[MergeCandidate]:
        stmt = (
            select(MergeCandidate)
            .join(Workspace, MergeCandidate.workspace_id == Workspace.id)
            .where(
                MergeCandidate.status == "open",
                ~Workspace.status.in_(
                    (
                        WorkspaceStatus.destroying.value,
                        WorkspaceStatus.destroyed.value,
                    )
                ),
            )
            .options(
                selectinload(MergeCandidate.attempt),
                selectinload(MergeCandidate.workspace).selectinload(Workspace.events),
                selectinload(MergeCandidate.task),
            )
            .order_by(Workspace.updated_at.desc(), Workspace.id.desc())
        )
        if repo_url is not None:
            stmt = stmt.where(MergeCandidate.repo_url == repo_url)
        if base_branch is not None:
            stmt = stmt.where(MergeCandidate.base_branch == base_branch)
        if status is not None:
            stmt = stmt.where(Workspace.status == status)
        if before_updated_at is not None and before_workspace_id is not None:
            stmt = stmt.where(
                or_(
                    Workspace.updated_at < before_updated_at,
                    and_(
                        Workspace.updated_at == before_updated_at,
                        Workspace.id < before_workspace_id,
                    ),
                )
            )
        stmt = stmt.limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def create_or_update_open_for_attempt(
        self,
        *,
        task: Task,
        attempt: TaskAttempt,
        workspace: Workspace,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> MergeCandidate:
        if not workspace.pr_url:
            raise ValueError("merge candidates require a workspace PR URL")

        candidate = await self.get_by_attempt_id(attempt.id)
        if candidate is None:
            candidate = MergeCandidate(
                id=new_merge_candidate_id(),
                task_id=task.id,
                attempt_id=attempt.id,
                workspace_id=workspace.id,
                pr_url=workspace.pr_url,
                pr_number=workspace.pr_number,
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                branch_name=workspace.branch_name,
                head_sha=head_sha,
                base_sha=base_sha,
                status="open",
            )
            self._session.add(candidate)
        else:
            candidate.task_id = task.id
            candidate.workspace_id = workspace.id
            candidate.pr_url = workspace.pr_url
            candidate.pr_number = workspace.pr_number
            candidate.repo_url = workspace.repo_url
            candidate.base_branch = workspace.branch_base
            candidate.branch_name = workspace.branch_name
            if head_sha is not None:
                candidate.head_sha = head_sha
            if base_sha is not None:
                candidate.base_sha = base_sha
            candidate.status = "open"
            candidate.close_reason = None
            candidate.closed_at = None
            candidate.merged_at = None

        sync_candidate_readiness(candidate, workspace=workspace, attempt=attempt)
        await self._session.flush()
        return candidate

    async def make_attempt_canonical_and_create_candidate(
        self,
        *,
        task: Task,
        attempt: TaskAttempt,
        workspace: Workspace,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> MergeCandidate:
        attempt_repo = TaskAttemptRepository(self._session)
        previous = await attempt_repo.mark_canonical_for_merge(attempt)
        if previous is not None and previous.id != attempt.id:
            await self.close_open_for_attempt(
                previous.id,
                close_reason="CANONICAL_CHANGED",
            )
        return await self.create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
            head_sha=head_sha,
            base_sha=base_sha,
        )

    async def close_open_for_attempt(
        self,
        attempt_id: str,
        *,
        close_reason: str,
    ) -> MergeCandidate | None:
        candidate = await self.get_by_attempt_id(attempt_id)
        if candidate is None or candidate.status != "open":
            return candidate
        candidate.status = "closed"
        candidate.close_reason = close_reason
        candidate.closed_at = datetime.now(UTC)
        sync_candidate_readiness(
            candidate,
            workspace=candidate.workspace,
            attempt=candidate.attempt,
        )
        await self._session.flush()
        return candidate

    async def close_open_for_workspace(
        self,
        workspace_id: str,
        *,
        close_reason: str,
    ) -> builtins.list[MergeCandidate]:
        stmt = (
            select(MergeCandidate)
            .where(
                MergeCandidate.workspace_id == workspace_id,
                MergeCandidate.status == "open",
            )
            .options(
                selectinload(MergeCandidate.workspace),
                selectinload(MergeCandidate.attempt),
            )
        )
        candidates = list((await self._session.execute(stmt)).scalars())
        now = datetime.now(UTC)
        for candidate in candidates:
            candidate.status = "closed"
            candidate.close_reason = close_reason
            candidate.closed_at = now
            sync_candidate_readiness(
                candidate,
                workspace=candidate.workspace,
                attempt=candidate.attempt,
            )
        await self._session.flush()
        return candidates

    async def mark_workspace_merged(self, workspace_id: str) -> MergeCandidate | None:
        stmt = (
            select(MergeCandidate)
            .where(
                MergeCandidate.workspace_id == workspace_id,
                MergeCandidate.status == "open",
            )
            .options(
                selectinload(MergeCandidate.workspace),
                selectinload(MergeCandidate.attempt),
            )
            .order_by(MergeCandidate.updated_at.desc(), MergeCandidate.id.desc())
            .limit(1)
        )
        candidate = (await self._session.execute(stmt)).scalar_one_or_none()
        if candidate is None:
            return None
        now = datetime.now(UTC)
        candidate.status = "merged"
        candidate.close_reason = None
        candidate.closed_at = None
        candidate.merged_at = now
        sync_candidate_readiness(
            candidate,
            workspace=candidate.workspace,
            attempt=candidate.attempt,
        )
        await self._session.flush()
        return candidate


class ValidationRunRepository:
    """CRUD helpers for durable validation provenance rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(
        self,
        *,
        workspace_id: str,
        attempt_id: str | None,
        tier: int,
        commands: list[dict[str, Any]],
        base_commit: str | None,
        target_branch: str | None,
        target_head_sha: str | None,
        log_stream_refs: dict[str, Any],
        base_sha: str | None = None,
        workspace_head_sha: str | None = None,
        profile_name: str | None = None,
        profile_version: int | None = None,
        profile_source: str | None = None,
        resolved_profile_digest: str | None = None,
        environment_identity_digest: str | None = None,
        environment_identity_inputs: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> ValidationRun:
        now = started_at or datetime.now(UTC)
        run = ValidationRun(
            id=new_validation_run_id(),
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            tier=tier,
            command_set_hash=validation_command_set_hash(commands),
            commands=commands,
            base_commit=base_commit,
            base_sha=base_sha,
            workspace_head_sha=workspace_head_sha,
            target_branch=target_branch,
            target_head_sha=target_head_sha,
            profile_name=profile_name,
            profile_version=profile_version,
            profile_source=profile_source,
            resolved_profile_digest=resolved_profile_digest,
            environment_identity_digest=environment_identity_digest,
            environment_identity_inputs=environment_identity_inputs,
            status="running",
            reason_code=None,
            started_at=now,
            finished_at=None,
            log_stream_refs=log_stream_refs,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def get(self, validation_run_id: str) -> ValidationRun | None:
        return await self._session.get(ValidationRun, validation_run_id)

    async def finish(
        self,
        validation_run_id: str,
        *,
        status: str,
        reason_code: str | None,
        finished_at: datetime | None = None,
        retry_count: int = 0,
        coverage: dict[str, Any] | None = None,
        command_retries: list[int] | None = None,
        coverage_evidence_status: str | None = None,
        coverage_evidence_reason_code: str | None = None,
        coverage_evidence_source_run_id: str | None = None,
    ) -> ValidationRun | None:
        run = await self.get(validation_run_id)
        if run is None:
            return None
        run.status = status
        run.reason_code = reason_code
        run.retry_count = retry_count
        run.finished_at = finished_at or datetime.now(UTC)
        if coverage is not None:
            coverage_payload = dict(coverage)
            run.coverage = coverage_payload
            log_stream_refs = dict(run.log_stream_refs or {})
            log_stream_refs["coverage"] = coverage_payload
            run.log_stream_refs = log_stream_refs
        if command_retries is not None:
            updated_commands = list(run.commands)
            for i, rc in enumerate(command_retries):
                if i < len(updated_commands):
                    updated_commands[i] = dict(updated_commands[i], retry_count=rc)
            run.commands = updated_commands
        if coverage_evidence_status is not None:
            updated_commands = list(run.commands)
            for i, command in enumerate(updated_commands):
                if command.get("phase") == "coverage":
                    updated = dict(command)
                    updated["evidence_status"] = coverage_evidence_status
                    if coverage_evidence_reason_code is not None:
                        updated["evidence_reason_code"] = coverage_evidence_reason_code
                    if coverage_evidence_source_run_id is not None:
                        updated["evidence_source_run_id"] = coverage_evidence_source_run_id
                    updated_commands[i] = updated
                    break
            run.commands = updated_commands
        await self._session.flush()
        return run

    async def update_target_head_sha(
        self,
        validation_run_id: str,
        *,
        target_head_sha: str | None,
        workspace_head_sha: str | None = None,
    ) -> ValidationRun | None:
        run = await self.get(validation_run_id)
        if run is None:
            return None
        run.target_head_sha = target_head_sha
        if workspace_head_sha is not None:
            run.workspace_head_sha = workspace_head_sha
        await self._session.flush()
        return run

    async def find_reusable_coverage_evidence(
        self,
        *,
        workspace_id: str,
        tier: int,
        commands: list[dict[str, Any]],
        workspace_head_sha: str | None,
        resolved_profile_digest: str | None,
        environment_identity_digest: str | None,
        max_age_seconds: int,
        now: datetime | None = None,
    ) -> ValidationRun | None:
        if not workspace_head_sha:
            return None
        cutoff = (now or datetime.now(UTC)) - timedelta(seconds=max_age_seconds)
        stmt = (
            select(ValidationRun)
            .where(
                ValidationRun.workspace_id == workspace_id,
                ValidationRun.tier == tier,
                ValidationRun.workspace_head_sha == workspace_head_sha,
                ValidationRun.command_set_hash == validation_command_set_hash(commands),
                ValidationRun.resolved_profile_digest == resolved_profile_digest,
                ValidationRun.environment_identity_digest == environment_identity_digest,
                ValidationRun.status == "succeeded",
                ValidationRun.finished_at.is_not(None),
                ValidationRun.finished_at >= cutoff,
            )
            .order_by(ValidationRun.finished_at.desc(), ValidationRun.id.desc())
            .limit(5)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        for row in rows:
            coverage = validation_run_coverage_payload(row)
            if coverage.get("status") in {
                "passed",
                "not_configured",
            } and not _coverage_metadata_has_pytest_failures(coverage):
                return row
        return None

    async def list_for_workspace(self, workspace_id: str) -> builtins.list[ValidationRun]:
        stmt = (
            select(ValidationRun)
            .where(ValidationRun.workspace_id == workspace_id)
            .order_by(ValidationRun.started_at, ValidationRun.id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_by_workspace_ids(
        self,
        workspace_ids: Iterable[str],
        *,
        status: str | None = None,
    ) -> dict[str, builtins.list[ValidationRun]]:
        unique_workspace_ids = tuple(dict.fromkeys(workspace_ids))
        if not unique_workspace_ids:
            return {}

        stmt = select(ValidationRun).where(ValidationRun.workspace_id.in_(unique_workspace_ids))
        if status is not None:
            stmt = stmt.where(ValidationRun.status == status)
        stmt = stmt.order_by(
            ValidationRun.workspace_id.asc(),
            ValidationRun.started_at.asc(),
            ValidationRun.id.asc(),
        )
        out: dict[str, builtins.list[ValidationRun]] = {
            workspace_id: [] for workspace_id in unique_workspace_ids
        }
        for run in (await self._session.execute(stmt)).scalars():
            out[run.workspace_id].append(run)
        return out

    async def latest_by_workspace_ids(
        self,
        workspace_ids: Iterable[str],
    ) -> dict[str, ValidationRun]:
        unique_workspace_ids = tuple(dict.fromkeys(workspace_ids))
        if not unique_workspace_ids:
            return {}

        ranked_runs = (
            select(
                ValidationRun.id.label("validation_run_id"),
                func.row_number()
                .over(
                    partition_by=ValidationRun.workspace_id,
                    order_by=(
                        ValidationRun.started_at.desc(),
                        ValidationRun.id.desc(),
                    ),
                )
                .label("run_rank"),
            )
            .where(ValidationRun.workspace_id.in_(unique_workspace_ids))
            .subquery()
        )
        stmt = (
            select(ValidationRun)
            .join(ranked_runs, ValidationRun.id == ranked_runs.c.validation_run_id)
            .where(ranked_runs.c.run_rank == 1)
        )
        return {run.workspace_id: run for run in (await self._session.execute(stmt)).scalars()}


class StaleReasonRepository:
    """CRUD helpers for the durable ``stale_reasons`` table.

    Used by the staleness refresh service to keep ``status='active'`` rows
    in lockstep with the latest evaluation against the target branch. Rows
    are flipped to ``status='resolved'`` (with ``resolved_at`` set) when a
    finding no longer applies — the historical row stays so console
    timelines can show recovery, not just degradation.
    """

    _ACTIVE = "active"
    _RESOLVED = "resolved"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active_for_candidate(
        self,
        candidate_id: str,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_candidate(candidate_id, status=self._ACTIVE)

    async def list_active_for_candidates(
        self,
        candidate_ids: Iterable[str],
    ) -> dict[str, builtins.list[StaleReason]]:
        unique_ids = tuple(dict.fromkeys(candidate_ids))
        if not unique_ids:
            return {}
        stmt = (
            select(StaleReason)
            .where(
                StaleReason.candidate_id.in_(unique_ids),
                StaleReason.status == self._ACTIVE,
            )
            .order_by(StaleReason.detected_at.asc(), StaleReason.id.asc())
        )
        rows = list((await self._session.execute(stmt)).scalars())
        out: dict[str, builtins.list[StaleReason]] = {cid: [] for cid in unique_ids}
        for row in rows:
            if row.candidate_id is None:  # pragma: no cover - filtered by WHERE
                continue
            out[row.candidate_id].append(row)
        return out

    async def list_for_candidate(
        self,
        candidate_id: str,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_candidate(candidate_id, status=None)

    async def list_active_for_workspace(
        self,
        workspace_id: str,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_workspace(workspace_id, status=self._ACTIVE)

    async def list_active_for_workspace_page(
        self,
        workspace_id: str,
        *,
        limit: int,
        offset: int,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_workspace(
            workspace_id,
            status=self._ACTIVE,
            limit=limit,
            offset=offset,
        )

    async def list_for_workspace(
        self,
        workspace_id: str,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_workspace(workspace_id, status=None)

    async def list_for_workspace_page(
        self,
        workspace_id: str,
        *,
        limit: int,
        offset: int,
    ) -> builtins.list[StaleReason]:
        return await self._list_for_workspace(
            workspace_id,
            status=None,
            limit=limit,
            offset=offset,
        )

    async def replace_active_findings(
        self,
        *,
        workspace_id: str,
        candidate_id: str,
        attempt_id: str | None,
        task_id: str | None,
        findings: builtins.list[StaleReasonCreate],
    ) -> tuple[builtins.list[StaleReason], builtins.list[StaleReason]]:
        """Make the active findings for ``candidate_id`` exactly match the
        supplied list. Returns ``(newly_added, newly_resolved)``.

        Idempotent: re-running with the same findings is a no-op (kept rows
        are not re-emitted as ``newly_added``).
        """
        existing_active = await self._list_for_candidate(
            candidate_id,
            status=self._ACTIVE,
        )
        finding_keys = {(f.reason_code, f.trigger_type, f.trigger_ref) for f in findings}
        now = datetime.now(UTC)

        newly_resolved: builtins.list[StaleReason] = []
        kept_keys: set[tuple[str, str, str | None]] = set()
        for row in existing_active:
            key = (row.reason_code, row.trigger_type, row.trigger_ref)
            if key in finding_keys:
                kept_keys.add(key)
                continue
            row.status = self._RESOLVED
            row.resolved_at = now
            newly_resolved.append(row)

        newly_added: builtins.list[StaleReason] = []
        for finding in findings:
            key = (finding.reason_code, finding.trigger_type, finding.trigger_ref)
            if key in kept_keys:
                continue
            row = StaleReason(
                id=new_stale_reason_id(),
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                task_id=task_id,
                trigger_type=finding.trigger_type,
                trigger_ref=finding.trigger_ref,
                reason_code=finding.reason_code,
                explanation=finding.explanation,
                status=self._ACTIVE,
                detected_at=now,
                resolved_at=None,
            )
            self._session.add(row)
            newly_added.append(row)

        await self._session.flush()
        return newly_added, newly_resolved

    async def _list_for_candidate(
        self,
        candidate_id: str,
        *,
        status: str | None,
    ) -> builtins.list[StaleReason]:
        stmt = select(StaleReason).where(StaleReason.candidate_id == candidate_id)
        if status is not None:
            stmt = stmt.where(StaleReason.status == status)
        stmt = stmt.order_by(StaleReason.detected_at.asc(), StaleReason.id.asc())
        return list((await self._session.execute(stmt)).scalars())

    async def _list_for_workspace(
        self,
        workspace_id: str,
        *,
        status: str | None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> builtins.list[StaleReason]:
        stmt = select(StaleReason).where(StaleReason.workspace_id == workspace_id)
        if status is not None:
            stmt = stmt.where(StaleReason.status == status)
        stmt = stmt.order_by(StaleReason.detected_at.asc(), StaleReason.id.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset is not None:
            stmt = stmt.offset(offset)
        return list((await self._session.execute(stmt)).scalars())


class PolicyFindingRepository:
    """CRUD helpers for durable workspace policy findings."""

    _ACTIVE = "active"
    _RESOLVED = "resolved"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active_for_workspace(
        self,
        workspace_id: str,
    ) -> builtins.list[PolicyFinding]:
        return await self._list_for_workspace(workspace_id, status=self._ACTIVE)

    async def list_for_workspace(
        self,
        workspace_id: str,
    ) -> builtins.list[PolicyFinding]:
        return await self._list_for_workspace(workspace_id, status=None)

    async def list_active_for_candidate(
        self,
        candidate_id: str,
    ) -> builtins.list[PolicyFinding]:
        return await self._list_for_candidate(candidate_id, status=self._ACTIVE)

    async def list_active_for_candidates(
        self,
        candidate_ids: Iterable[str],
    ) -> dict[str, builtins.list[PolicyFinding]]:
        unique_ids = tuple(dict.fromkeys(candidate_ids))
        if not unique_ids:
            return {}
        stmt = (
            select(PolicyFinding)
            .where(
                PolicyFinding.candidate_id.in_(unique_ids),
                PolicyFinding.status == self._ACTIVE,
            )
            .order_by(PolicyFinding.detected_at.asc(), PolicyFinding.id.asc())
        )
        rows = list((await self._session.execute(stmt)).scalars())
        out: dict[str, builtins.list[PolicyFinding]] = {cid: [] for cid in unique_ids}
        for row in rows:
            if row.candidate_id is None:  # pragma: no cover - filtered by WHERE
                continue
            out[row.candidate_id].append(row)
        return out

    async def replace_active_findings(
        self,
        *,
        workspace_id: str,
        candidate_id: str | None,
        attempt_id: str | None,
        task_id: str | None,
        reason_code: str,
        findings: builtins.list[PolicyFindingCreate],
    ) -> tuple[builtins.list[PolicyFinding], builtins.list[PolicyFinding]]:
        """Make active findings for ``reason_code`` exactly match ``findings``.

        Historical rows are resolved rather than deleted so operator timelines
        can show when a policy issue appeared and when it cleared.
        """
        existing_active = await self._list_for_subject(
            workspace_id=workspace_id,
            candidate_id=candidate_id,
            reason_code=reason_code,
            status=self._ACTIVE,
        )
        finding_keys = {
            (
                f.reason_code,
                f.severity,
                f.subject_path,
                json.dumps(f.details, sort_keys=True, separators=(",", ":")),
            )
            for f in findings
        }
        now = datetime.now(UTC)

        newly_resolved: builtins.list[PolicyFinding] = []
        kept_keys: set[tuple[str, str, str | None, str]] = set()
        for row in existing_active:
            key = (
                row.reason_code,
                row.severity,
                row.subject_path,
                json.dumps(row.details, sort_keys=True, separators=(",", ":")),
            )
            if key in finding_keys:
                kept_keys.add(key)
                continue
            row.status = self._RESOLVED
            row.resolved_at = now
            newly_resolved.append(row)

        newly_added: builtins.list[PolicyFinding] = []
        for finding in findings:
            key = (
                finding.reason_code,
                finding.severity,
                finding.subject_path,
                json.dumps(finding.details, sort_keys=True, separators=(",", ":")),
            )
            if key in kept_keys:
                continue
            row = PolicyFinding(
                id=new_policy_finding_id(),
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                task_id=task_id,
                reason_code=finding.reason_code,
                severity=finding.severity,
                subject_path=finding.subject_path,
                explanation=finding.explanation,
                details=finding.details,
                status=self._ACTIVE,
                detected_at=now,
                resolved_at=None,
            )
            self._session.add(row)
            newly_added.append(row)

        await self._session.flush()
        return newly_added, newly_resolved

    async def _list_for_candidate(
        self,
        candidate_id: str,
        *,
        status: str | None,
    ) -> builtins.list[PolicyFinding]:
        stmt = select(PolicyFinding).where(PolicyFinding.candidate_id == candidate_id)
        if status is not None:
            stmt = stmt.where(PolicyFinding.status == status)
        stmt = stmt.order_by(PolicyFinding.detected_at.asc(), PolicyFinding.id.asc())
        return list((await self._session.execute(stmt)).scalars())

    async def _list_for_workspace(
        self,
        workspace_id: str,
        *,
        status: str | None,
    ) -> builtins.list[PolicyFinding]:
        stmt = select(PolicyFinding).where(PolicyFinding.workspace_id == workspace_id)
        if status is not None:
            stmt = stmt.where(PolicyFinding.status == status)
        stmt = stmt.order_by(PolicyFinding.detected_at.asc(), PolicyFinding.id.asc())
        return list((await self._session.execute(stmt)).scalars())

    async def _list_for_subject(
        self,
        *,
        workspace_id: str,
        candidate_id: str | None,
        reason_code: str,
        status: str | None,
    ) -> builtins.list[PolicyFinding]:
        stmt = select(PolicyFinding).where(
            PolicyFinding.workspace_id == workspace_id,
            PolicyFinding.reason_code == reason_code,
        )
        if candidate_id is None:
            stmt = stmt.where(PolicyFinding.candidate_id.is_(None))
        else:
            stmt = stmt.where(PolicyFinding.candidate_id == candidate_id)
        if status is not None:
            stmt = stmt.where(PolicyFinding.status == status)
        stmt = stmt.order_by(PolicyFinding.detected_at.asc(), PolicyFinding.id.asc())
        return list((await self._session.execute(stmt)).scalars())


class PRFeedbackResolutionRepository:
    """PR-scoped review feedback verdicts shared by replacement workspaces."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def record_resolution(
        self,
        *,
        scm_provider: str,
        repository_key: str,
        pull_request_key: str,
        pull_request_url: str | None,
        head_sha: str,
        feedback_kind: str,
        feedback_id: str,
        feedback_body: str | None,
        feedback_author: str | None,
        feedback_url: str | None,
        verdict: str,
        reason: str | None,
        source_workspace_id: str | None,
        source_operation_id: str | None = None,
        resolved_at: datetime | None = None,
    ) -> PRFeedbackResolution:
        now = resolved_at or datetime.now(UTC)
        values: dict[str, Any] = {
            "scm_provider": _normalize_provider_key(scm_provider),
            "repository_key": _normalize_repository_key(repository_key),
            "pull_request_key": pull_request_key.strip(),
            "pull_request_url": pull_request_url,
            "head_sha": head_sha.strip(),
            "feedback_kind": feedback_kind.strip().lower(),
            "feedback_id": feedback_id.strip(),
            "feedback_body_hash": pr_feedback_body_hash(feedback_body),
            "feedback_url": feedback_url,
            "feedback_author": feedback_author,
            "verdict": verdict.strip(),
            "reason": reason.strip()[:2048] if reason else None,
            "source_workspace_id": source_workspace_id,
            "source_operation_id": source_operation_id,
            "resolved_at": now,
            "created_at": now,
            "updated_at": now,
        }
        stmt = _pr_feedback_resolution_upsert_stmt(self._dialect_name)
        if stmt is None:
            raise RuntimeError("PR feedback resolution persistence requires PostgreSQL")
        row = cast(
            PRFeedbackResolution,
            (await self._session.execute(stmt.values(**values))).scalar_one(),
        )
        await self._session.flush()
        return row

    async def get(
        self,
        *,
        scm_provider: str,
        repository_key: str,
        pull_request_key: str,
        feedback_kind: str,
        feedback_id: str,
        feedback_body_hash: str,
    ) -> PRFeedbackResolution | None:
        stmt = select(PRFeedbackResolution).where(
            PRFeedbackResolution.scm_provider == _normalize_provider_key(scm_provider),
            PRFeedbackResolution.repository_key == _normalize_repository_key(repository_key),
            PRFeedbackResolution.pull_request_key == pull_request_key.strip(),
            PRFeedbackResolution.feedback_kind == feedback_kind.strip().lower(),
            PRFeedbackResolution.feedback_id == feedback_id.strip(),
            PRFeedbackResolution.feedback_body_hash == feedback_body_hash,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_pr(
        self,
        *,
        scm_provider: str,
        repository_key: str,
        pull_request_key: str,
    ) -> list[PRFeedbackResolution]:
        stmt = (
            select(PRFeedbackResolution)
            .where(
                PRFeedbackResolution.scm_provider == _normalize_provider_key(scm_provider),
                PRFeedbackResolution.repository_key == _normalize_repository_key(repository_key),
                PRFeedbackResolution.pull_request_key == pull_request_key.strip(),
            )
            .order_by(PRFeedbackResolution.updated_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars())


def sync_candidate_readiness(
    candidate: MergeCandidate,
    *,
    workspace: Workspace,
    attempt: TaskAttempt,
    sync_validation_staleness: bool = True,
    recompute_stale: bool | None = None,
) -> None:
    from awf.runtime.merge_eligibility import (
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        compute_stale_reason,
    )

    if recompute_stale is not None:
        sync_validation_staleness = recompute_stale

    workspace_status = WorkspaceStatus(workspace.status)
    is_open = candidate.status == "open"
    is_canonical = attempt.is_canonical_for_merge
    is_completed = candidate.status == "merged" or workspace_status == WorkspaceStatus.completed
    failed_or_cancelled = workspace_status in {
        WorkspaceStatus.failed,
        WorkspaceStatus.cancelled,
    }
    not_canonical = not is_canonical

    if sync_validation_staleness:
        scope_stale_reason = _initial_scope_stale_reason(workspace)
        stale_reason, _ = compute_stale_reason(workspace)
        # If there's an active stale reason, update it. If the reason clears, clear it.
        if scope_stale_reason is not None:
            candidate.stale = True
            candidate.stale_reason = scope_stale_reason
        elif stale_reason is not None:
            candidate.stale = True
            candidate.stale_reason = stale_reason
        elif candidate.stale_reason in (
            VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
            VALIDATION_MISSING_FOR_CURRENT_HEAD_STALE_REASON,
            DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
        ):
            candidate.stale = False
            candidate.stale_reason = None

    candidate.completed = is_completed
    candidate.failed_or_cancelled = failed_or_cancelled
    candidate.not_canonical = not_canonical
    candidate.waiting_for_monitor = is_open and workspace_status == WorkspaceStatus.pushing
    candidate.manual_merge_required = (
        is_open
        and workspace_status == WorkspaceStatus.monitoring_pr
        and not workspace.auto_merge
        and is_canonical
        and not candidate.policy_blocked
        and not candidate.stale
    )
    candidate.ready = (
        is_open
        and workspace_status == WorkspaceStatus.monitoring_pr
        and workspace.auto_merge
        and is_canonical
        and not candidate.policy_blocked
        and not candidate.stale
    )
    if not is_open:
        candidate.ready = False
        candidate.waiting_for_monitor = False
        candidate.manual_merge_required = False
    if is_completed:
        candidate.ready = False
        candidate.waiting_for_monitor = False
        candidate.manual_merge_required = False
        candidate.failed_or_cancelled = False
        candidate.stale = False
        candidate.stale_reason = None


def _initial_scope_stale_reason(workspace: Workspace) -> str | None:
    if workspace.task_class != TaskClass.docs_task.value:
        return None
    if not _claims_non_docs_path(workspace.owned_paths):
        return None
    return DOCS_TASK_SCOPE_VIOLATION_STALE_REASON


def _claims_non_docs_path(owned_paths: list[str] | tuple[str, ...]) -> bool:
    for path in owned_paths:
        normalized = path.strip().replace("\\", "/")
        if not normalized:
            continue
        if normalized.startswith("docs/"):
            continue
        if normalized in {"README.md", "CHANGELOG.md", "CONTRIBUTING.md"}:
            continue
        if normalized.endswith((".md", ".mdx", ".rst")):
            continue
        return True
    return False
