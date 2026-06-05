"""Workspace, WorkspaceEvent, Operation, LogStream, and SecretLease database repositories."""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import (
    and_,
    case,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from awf.common.audit import build_audit_payload
from awf.common.ids import (
    new_event_id,
)
from awf.db.enums import (
    AgentRuntime,
    WorkspaceStatus,
)
from awf.db.models import (
    MergeCandidate,
    TaskAttempt,
    Workspace,
    WorkspaceEvent,
)
from awf.db.repositories._scheduler import (
    _schedulable_workspace_ids_stmt,
    _scheduler_scoring_time,
)
from awf.db.repositories.base import (
    DEFAULT_IDEMPOTENCY_REPLAY_KEY_LIMIT,
    HostPortConflict,
    OwnedPathConflict,
    OwnedPathOverlap,
    WorkspaceEventCreate,
    _candidate_terminal_close_reason,
    _matches_pr_adoption_identity,
    _releases_resource_reservation,
    _workspace_idempotency_advisory_lock_key,
    _workspace_status_value,
    resolve_session_dialect_name,
)
from awf.db.repositories.quality_repo import (
    MergeCandidateRepository,
    ResourceReservationRepository,
)
from awf.db.repositories.task_repo import (
    TaskAttemptRepository,
    TaskRepository,
)
from awf.db.repositories.workspace_repo_claims import (
    claim_monitoring_pr,
    claim_worker_restart_recovery_execution,
    read_execution_claim_epoch,
    refresh_execution_claim,
    refresh_monitoring_pr_claim,
    release_execution_claim,
    release_monitoring_pr_claim,
)
from awf.db.repositories.workspace_repo_host_ports import (
    acquire_host_port_admission_lock,
    acquire_owned_path_conflict_lock,
    find_active_owned_path_conflicts,
    find_active_owned_path_overlaps,
    find_host_port_conflicts,
)


def _transition_releases_provisioning_execution_claim(
    old_state: str,
    to: WorkspaceStatus,
) -> bool:
    return old_state == WorkspaceStatus.provisioning.value and to != WorkspaceStatus.provisioning


class _RepositoriesProxy:
    @property
    def set_committed_value(self) -> Any:
        import awf.db.repositories as repo

        return repo.set_committed_value

    @property
    def new_workspace_id(self) -> Any:
        import awf.db.repositories as repo

        return repo.new_workspace_id


_repo_proxy = _RepositoriesProxy()


def set_committed_value(instance: object, key: str, value: Any) -> None:
    """Proxy SQLAlchemy committed-value updates for test patching."""
    _repo_proxy.set_committed_value(instance, key, value)


def new_workspace_id() -> str:
    """Return a fresh workspace identifier through the repository proxy."""
    return cast(str, _repo_proxy.new_workspace_id())


class WorkspaceRepository:
    """CRUD + state transitions for workspaces.

    Holds a reference to an ``AsyncSession`` for the life of one logical unit
    of work. Do not reuse across request boundaries.
    """

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        """Initialize the repository with a session and resolved dialect name."""
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    @property
    def dialect_name(self) -> str | None:
        """Return the SQL dialect name used by this repository."""
        return self._dialect_name

    async def create(
        self,
        *,
        repo_url: str,
        branch_base: str,
        task_title: str,
        task_prompt: str,
        agent: str,
        test_commands: list[str],
        requires_database: bool = False,
        task_external_id: str | None = None,
        task_class: str | None = None,
        owned_paths: list[str] | None = None,
        task_policy: dict[str, Any] | None = None,
        auto_merge: bool = True,
        initial_review_grace_period_seconds: float | None = None,
        env_profile: str | None = None,
        profile_ref: str | None = None,
        requested_profile: dict[str, Any] | None = None,
        resolved_profile: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        task_kind: str = "feature_branch_pr",
        remote_push_branch: str | None = None,
        workspace_id: str | None = None,
    ) -> Workspace:
        """Create a new workspace in ``requested`` status and emit a creation event.

        Does not commit — the caller owns the transaction boundary.
        """
        workspace = Workspace(
            id=workspace_id or new_workspace_id(),
            status=WorkspaceStatus.requested.value,
            version=1,
            event_sequence=1,
            repo_url=repo_url,
            branch_base=branch_base,
            remote_push_branch=remote_push_branch,
            task_title=task_title,
            task_prompt=task_prompt,
            task_external_id=task_external_id,
            task_class=task_class,
            task_kind=task_kind,
            owned_paths=list(owned_paths or []),
            task_policy=dict(task_policy or {}),
            auto_merge=auto_merge,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            agent=agent,
            env_profile=env_profile,
            profile_ref=profile_ref,
            requested_profile=requested_profile,
            resolved_profile=resolved_profile,
            test_commands=test_commands,
            requires_database=requires_database,
            idempotency_key=idempotency_key,
        )
        workspace.operations = []
        workspace.events.append(
            WorkspaceEvent(
                id=new_event_id(),
                event_type="workspace.created",
                old_state=None,
                new_state=WorkspaceStatus.requested.value,
                reason_code="CREATED",
                event_order=workspace.event_sequence,
            )
        )
        self._session.add(workspace)
        await self._session.flush()
        return workspace

    async def create_replacement_from(
        self,
        source: Workspace,
        *,
        idempotency_key: str | None = None,
        task_prompt: str | None = None,
        task_policy: Mapping[str, Any] | None = None,
        agent: str | None = None,
        remote_push_branch: str | None = None,
    ) -> Workspace:
        """Create a fresh requested workspace from another workspace's request contract.

        Runtime, PR-monitor, and resolved profile fields stay on the source.
        Pass ``remote_push_branch`` only for replacement flows that must
        preserve an existing external target branch.
        """
        return await self.create(
            repo_url=source.repo_url,
            branch_base=source.branch_base,
            task_title=source.task_title,
            task_prompt=source.task_prompt if task_prompt is None else task_prompt,
            task_external_id=source.task_external_id,
            task_class=source.task_class,
            owned_paths=list(source.owned_paths),
            task_policy=deepcopy(
                dict(task_policy if task_policy is not None else source.task_policy)
            ),
            auto_merge=source.auto_merge,
            initial_review_grace_period_seconds=source.initial_review_grace_period_seconds,
            agent=source.agent if agent is None else agent,
            env_profile=source.env_profile,
            profile_ref=source.profile_ref,
            requested_profile=deepcopy(source.requested_profile),
            resolved_profile=None,
            test_commands=list(source.test_commands),
            requires_database=source.requires_database,
            idempotency_key=idempotency_key,
            task_kind=source.task_kind,
            remote_push_branch=remote_push_branch,
        )

    async def update_activity(self, workspace_id: str, *, subphase: str | None = None) -> None:
        """Stamp recent workspace activity and optionally update its subphase."""
        stmt = (
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                last_activity_at=datetime.now(UTC),
            )
        )
        if subphase is not None:
            stmt = stmt.values(subphase=subphase)
        await self._session.execute(stmt)
        await self._session.flush()

    async def get(self, workspace_id: str) -> Workspace | None:
        """Return a workspace by primary key, if it exists."""
        return await self._session.get(Workspace, workspace_id)

    async def get_with_secret_leases(self, workspace_id: str) -> Workspace | None:
        """Return a workspace with secret leases and operations loaded."""
        stmt = (
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .options(selectinload(Workspace.secret_leases))
            .options(selectinload(Workspace.operations))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_with_operations(self, workspace_id: str) -> Workspace | None:
        """Return a workspace with operations eagerly loaded."""
        stmt = (
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .options(selectinload(Workspace.operations))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_with_validation_runs(self, workspace_id: str) -> Workspace | None:
        """Return a workspace with validation runs eagerly loaded."""
        stmt = (
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .options(selectinload(Workspace.validation_runs))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_for_update(
        self,
        workspace_id: str,
        *,
        skip_locked: bool = False,
    ) -> Workspace | None:
        """Load one workspace with a row lock when the database supports it.

        When ``skip_locked=True`` and the row is currently locked by another
        transaction, returns ``None`` instead of waiting (Postgres
        ``SELECT FOR UPDATE SKIP LOCKED``).
        """
        stmt = select(Workspace).where(Workspace.id == workspace_id)
        if self._dialect_name == "postgresql":
            stmt = stmt.with_for_update(of=Workspace, skip_locked=skip_locked)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def exists(self, workspace_id: str) -> bool:
        """Return whether a workspace row exists."""
        stmt = select(Workspace.id).where(Workspace.id == workspace_id).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def get_by_idempotency_key(self, key: str) -> Workspace | None:
        """Return a workspace created for an idempotency key."""
        stmt = (
            select(Workspace)
            .where(Workspace.idempotency_key == key)
            .options(selectinload(Workspace.task_attempt))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def has_idempotency_key(self, key: str) -> bool:
        """Return whether an idempotency key already has a workspace."""
        stmt = select(Workspace.id).where(Workspace.idempotency_key == key).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def list_idempotency_replay_keys(
        self,
        *,
        limit: int = DEFAULT_IDEMPOTENCY_REPLAY_KEY_LIMIT,
    ) -> builtins.list[str]:
        """Return a bounded replay-key sample for non-request-path cache support.

        Fresh request admission paths use exact-key probes instead of this helper
        so over-limit requests cannot trigger broad replay-key warmups.
        """
        if limit <= 0:
            return []
        stmt = (
            select(Workspace.idempotency_key)
            .where(Workspace.idempotency_key.is_not(None))
            .order_by(Workspace.created_at.asc(), Workspace.id.asc())
            .limit(limit)
        )
        return [key for key in (await self._session.execute(stmt)).scalars().all() if key]

    async def list_idempotency_key_family(self, logical_key: str) -> builtins.list[str]:
        """Return the logical idempotency key and any generation-suffixed keys."""
        from awf.db.utils import escape_like_pattern as _escape_like_pattern

        generation_pattern = f"{_escape_like_pattern(logical_key)}:g%"
        stmt = (
            select(Workspace.idempotency_key)
            .where(
                or_(
                    Workspace.idempotency_key == logical_key,
                    Workspace.idempotency_key.like(generation_pattern, escape="\\"),
                )
            )
            .order_by(Workspace.idempotency_key.asc())
        )
        keys = (await self._session.execute(stmt)).scalars().all()
        return [key for key in keys if key is not None]

    async def list_pr_adoption_history(
        self,
        *,
        task_external_id: str,
        idempotency_key: str,
        task_kind: str,
        repo_slug: str,
        pr_number: int,
    ) -> builtins.list[Workspace]:
        """List workspaces that represent adoption history for one repo/PR."""
        adoption_repo_slug = Workspace.task_policy["pr_adoption"]["repo_slug"].as_string()
        stmt = (
            select(Workspace)
            .options(selectinload(Workspace.task_attempt))
            .where(
                or_(
                    Workspace.task_external_id == task_external_id,
                    Workspace.idempotency_key == idempotency_key,
                    and_(
                        Workspace.task_kind == task_kind,
                        Workspace.pr_number == pr_number,
                        func.lower(adoption_repo_slug) == repo_slug.lower(),
                    ),
                )
            )
            .order_by(Workspace.created_at.asc(), Workspace.id.asc())
        )
        candidates = list((await self._session.execute(stmt)).scalars())
        return [
            workspace
            for workspace in candidates
            if _matches_pr_adoption_identity(
                workspace,
                task_external_id=task_external_id,
                idempotency_key=idempotency_key,
                task_kind=task_kind,
                repo_slug=repo_slug,
                pr_number=pr_number,
            )
        ]

    async def acquire_idempotency_key_lock(self, key: str) -> None:
        """Serialize workspace idempotency decisions with a PostgreSQL advisory lock."""
        lock_key = _workspace_idempotency_advisory_lock_key(key)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def acquire_owned_path_conflict_lock(
        self,
        *,
        repo_url: str,
        branch_base: str,
        owned_paths: list[str],
    ) -> None:
        """Acquire advisory locks for the given owned paths to serialize conflict checks."""
        return await acquire_owned_path_conflict_lock(
            self._session,
            self._dialect_name,
            repo_url=repo_url,
            branch_base=branch_base,
            owned_paths=owned_paths,
        )

    async def acquire_host_port_admission_lock(
        self,
        *,
        host_ports: list[int],
    ) -> None:
        """Acquire per-host-port advisory locks for workspace admission."""
        return await acquire_host_port_admission_lock(
            self._session, self._dialect_name, host_ports=host_ports
        )

    async def find_active_owned_path_conflicts(
        self,
        *,
        repo_url: str,
        branch_base: str,
        owned_paths: list[str],
        resolved_profile: Mapping[str, object] | None = None,
        workspace_id: str | None = None,
    ) -> list[OwnedPathConflict]:
        """Find active workspaces whose owned paths conflict with the given set."""
        return await find_active_owned_path_conflicts(
            self._session,
            repo_url=repo_url,
            branch_base=branch_base,
            owned_paths=owned_paths,
            resolved_profile=resolved_profile,
            workspace_id=workspace_id,
        )

    async def find_active_owned_path_overlaps(
        self,
        *,
        repo_url: str,
        branch_base: str,
        owned_paths: list[str],
        resolved_profile: Mapping[str, object] | None = None,
        workspace_id: str | None = None,
    ) -> list[OwnedPathOverlap]:
        """Find active workspaces whose owned paths overlap with the given set."""
        return await find_active_owned_path_overlaps(
            self._session,
            repo_url=repo_url,
            branch_base=branch_base,
            owned_paths=owned_paths,
            resolved_profile=resolved_profile,
            workspace_id=workspace_id,
        )

    async def find_host_port_conflicts(
        self,
        *,
        host_ports: builtins.list[int],
        excluding_workspace_id: str | None = None,
        node_id: str | None = None,
    ) -> builtins.list[HostPortConflict]:
        """Find active workspaces whose host ports conflict with the given set."""
        return await find_host_port_conflicts(
            self._session,
            host_ports=host_ports,
            excluding_workspace_id=excluding_workspace_id,
            node_id=node_id,
        )

    async def list(
        self,
        *,
        status: WorkspaceStatus
        | str
        | builtins.list[WorkspaceStatus]
        | builtins.list[str]
        | None = None,
        agent: AgentRuntime | str | None = None,
        repo_url: str | None = None,
        before_created_at: datetime | None = None,
        before_workspace_id: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Workspace]:
        """List workspaces with optional status, agent, repo, and cursor filters."""
        stmt = select(Workspace)
        if status is not None:
            if isinstance(status, builtins.list):
                stmt = stmt.where(
                    Workspace.status.in_(
                        [s.value if isinstance(s, WorkspaceStatus) else s for s in status]
                    )
                )
            else:
                stmt = stmt.where(Workspace.status == status)
        if agent is not None:
            stmt = stmt.where(Workspace.agent == agent)
        if repo_url is not None:
            stmt = stmt.where(Workspace.repo_url == repo_url)
        if before_created_at is not None and before_workspace_id is not None:
            stmt = stmt.where(
                or_(
                    Workspace.created_at < before_created_at,
                    and_(
                        Workspace.created_at == before_created_at,
                        Workspace.id < before_workspace_id,
                    ),
                )
            )
        stmt = (
            stmt.order_by(Workspace.created_at.desc(), Workspace.id.desc())
            .options(selectinload(Workspace.operations))
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_without_task_attempts(
        self,
        *,
        status: WorkspaceStatus | str | None = None,
        agent: AgentRuntime | str | None = None,
        repo_url: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Workspace]:
        """List workspaces that do not yet have task attempts."""
        stmt = (
            select(Workspace)
            .outerjoin(TaskAttempt, TaskAttempt.workspace_id == Workspace.id)
            .where(TaskAttempt.id.is_(None))
        )
        if status is not None:
            stmt = stmt.where(Workspace.status == status)
        if agent is not None:
            stmt = stmt.where(Workspace.agent == agent)
        if repo_url is not None:
            stmt = stmt.where(Workspace.repo_url == repo_url)
        stmt = (
            stmt.order_by(Workspace.created_at.desc(), Workspace.id.desc())
            .options(selectinload(Workspace.operations))
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_merge_queue(
        self,
        *,
        repo_url: str | None = None,
        base_branch: str | None = None,
        status: WorkspaceStatus | str | None = None,
        before_updated_at: datetime | None = None,
        before_workspace_id: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Workspace]:
        """List legacy PR workspaces visible in the merge queue."""
        stmt = (
            select(Workspace)
            .where(
                Workspace.pr_url.is_not(None),
                Workspace.pr_url != "",
                ~Workspace.status.in_(
                    (
                        WorkspaceStatus.destroying.value,
                        WorkspaceStatus.destroyed.value,
                    )
                ),
            )
            .order_by(Workspace.updated_at.desc(), Workspace.id.desc())
        )
        if repo_url is not None:
            stmt = stmt.where(Workspace.repo_url == repo_url)
        if base_branch is not None:
            stmt = stmt.where(Workspace.branch_base == base_branch)
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

    async def list_merge_queue_without_candidates(
        self,
        *,
        repo_url: str | None = None,
        base_branch: str | None = None,
        status: WorkspaceStatus | str | None = None,
        before_updated_at: datetime | None = None,
        before_workspace_id: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Workspace]:
        """List legacy merge-queue workspaces not backed by merge candidates."""
        stmt = (
            select(Workspace)
            .outerjoin(MergeCandidate, MergeCandidate.workspace_id == Workspace.id)
            .where(
                MergeCandidate.id.is_(None),
                Workspace.pr_url.is_not(None),
                Workspace.pr_url != "",
                ~Workspace.status.in_(
                    (
                        WorkspaceStatus.destroying.value,
                        WorkspaceStatus.destroyed.value,
                    )
                ),
            )
            .order_by(Workspace.updated_at.desc(), Workspace.id.desc())
        )
        if repo_url is not None:
            stmt = stmt.where(Workspace.repo_url == repo_url)
        if base_branch is not None:
            stmt = stmt.where(Workspace.branch_base == base_branch)
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

    async def list_schedulable_ids(
        self,
        *,
        status: WorkspaceStatus,
        limit: int,
        exclude_ids: set[str] | None = None,
        node_id: str | None = None,
        after: Any | None = None,
        scoring_at: datetime | None = None,
    ) -> builtins.list[str]:
        """Return schedulable workspace IDs in scheduler priority order."""
        if limit <= 0:
            return []

        scoring_time = _scheduler_scoring_time(after=after, scoring_at=scoring_at)
        candidates = await self._list_schedulable_candidates(
            status=status,
            limit=limit,
            exclude_ids=exclude_ids,
            node_id=node_id,
            after=after,
            scoring_at=scoring_time,
        )
        return [
            workspace.id
            for workspace in self._sort_schedulable_workspaces(
                candidates,
                limit,
                scoring_at=scoring_time,
            )
        ]

    async def list_schedulable_workspaces(
        self,
        *,
        status: WorkspaceStatus,
        limit: int,
        exclude_ids: set[str] | None = None,
        node_id: str | None = None,
        after: Any | None = None,
        scoring_at: datetime | None = None,
    ) -> builtins.list[Workspace]:
        """Return schedulable workspaces in scheduler priority order."""
        if limit <= 0:
            return []

        scoring_time = _scheduler_scoring_time(after=after, scoring_at=scoring_at)
        candidates = await self._list_schedulable_candidates(
            status=status,
            limit=limit,
            exclude_ids=exclude_ids,
            node_id=node_id,
            after=after,
            scoring_at=scoring_time,
        )

        return self._sort_schedulable_workspaces(
            candidates,
            limit,
            scoring_at=scoring_time,
        )

    async def _list_schedulable_candidates(
        self,
        *,
        status: WorkspaceStatus,
        limit: int | None,
        exclude_ids: set[str] | None = None,
        node_id: str | None = None,
        after: Any | None = None,
        scoring_at: datetime,
    ) -> builtins.list[Workspace]:
        stmt = _schedulable_workspace_ids_stmt(
            status=status,
            limit=limit,
            exclude_ids=exclude_ids,
            node_id=node_id,
            after=after,
            scoring_at=scoring_at,
            dialect_name=self._dialect_name,
            skip_locked=self._dialect_name == "postgresql",
            claim_cutoff=datetime.now(UTC) if status == WorkspaceStatus.monitoring_pr else None,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def _sort_schedulable_workspaces(
        candidates: builtins.list[Workspace],
        limit: int | None,
        *,
        scoring_at: datetime,
    ) -> builtins.list[Workspace]:
        from awf.service.scheduler import (
            scheduler_score_from_workspace,
        )

        # Handle paginated order key fallback
        def _get_sort_key(item: tuple[Any, Workspace]) -> tuple[Any, ...]:
            score, ws = item
            # Order tuple representation matching DB schema ordering:
            # (class_priority, effective_score, queued_at, workspace_id)
            return (
                -score.class_priority,
                -score.effective_score,
                ws.created_at or datetime.min,
                ws.id,
            )

        scored = sorted(
            (
                (scheduler_score_from_workspace(workspace, now=scoring_at), workspace)
                for workspace in candidates
            ),
            key=_get_sort_key,
        )
        ordered = [workspace for _score, workspace in scored]
        return ordered if limit is None else ordered[:limit]

    async def transition(
        self,
        workspace: Workspace,
        *,
        to: WorkspaceStatus,
        reason_code: str,
        payload: dict[str, Any] | None = None,
    ) -> Workspace:
        """Move a workspace to the given status, recording an event.

        Validates the transition through ``WorkspaceStateMachine``. Bumps
        ``version`` for optimistic concurrency on downstream updates.
        """
        from awf.control.state_machine import WorkspaceStateMachine

        current = WorkspaceStatus(workspace.status)
        WorkspaceStateMachine.assert_transition(current, to)

        event_order = await self._reserve_workspace_event_orders(
            workspace,
            count=1,
            bump_version=True,
        )
        old_state = workspace.status
        workspace.status = to.value
        if _transition_releases_provisioning_execution_claim(old_state, to):
            workspace.execution_claimed_by = None
            workspace.execution_claim_expires_at = None
        attempt = await TaskAttemptRepository(
            self._session,
            dialect_name=self._dialect_name,
        ).get_by_workspace_id(workspace.id)
        if attempt is not None:
            attempt.status = to.value
        if to == WorkspaceStatus.monitoring_pr and workspace.monitor_started_at is None:
            workspace.monitor_started_at = datetime.now(UTC)
        await self._sync_merge_candidate_lifecycle(workspace, attempt=attempt, to=to)
        if _releases_resource_reservation(to):
            await ResourceReservationRepository(self._session).release_active_for_workspace(
                workspace.id
            )

        workspace.events.append(
            WorkspaceEvent(
                id=new_event_id(),
                event_type="workspace.state_changed",
                old_state=old_state,
                new_state=to.value,
                reason_code=reason_code,
                payload=payload,
                event_order=event_order,
            )
        )
        return workspace

    async def transition_if_current(
        self,
        workspace_id: str,
        *,
        from_status: WorkspaceStatus,
        to: WorkspaceStatus,
        reason_code: str,
        payload: dict[str, Any] | None = None,
        extra_conditions: tuple[ColumnElement[bool], ...] = (),
    ) -> Workspace | None:
        """Atomically transition a row only if it is still in ``from_status``."""
        from awf.control.state_machine import WorkspaceStateMachine

        WorkspaceStateMachine.assert_transition(from_status, to)

        now = datetime.now(UTC)
        if self._dialect_name != "postgresql":
            return await self._transition_if_current_with_atomic_update(
                workspace_id,
                from_status=from_status,
                to=to,
                reason_code=reason_code,
                payload=payload,
                extra_conditions=extra_conditions,
                now=now,
            )

        stmt = (
            select(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.status == from_status.value,
                *extra_conditions,
            )
            .execution_options(populate_existing=True)
        )
        stmt = stmt.with_for_update(of=Workspace)
        workspace = (await self._session.execute(stmt)).scalar_one_or_none()
        if workspace is None:
            return None

        event_order = await self._reserve_workspace_event_orders(
            workspace,
            count=1,
            bump_version=True,
        )
        old_state = workspace.status
        workspace.status = to.value
        workspace.updated_at = now
        if _transition_releases_provisioning_execution_claim(old_state, to):
            workspace.execution_claimed_by = None
            workspace.execution_claim_expires_at = None
        return await self._finish_transition_if_current(
            workspace,
            old_state=old_state,
            to=to,
            reason_code=reason_code,
            payload=payload,
            event_order=event_order,
            now=now,
        )

    async def _transition_if_current_with_atomic_update(
        self,
        workspace_id: str,
        *,
        from_status: WorkspaceStatus,
        to: WorkspaceStatus,
        reason_code: str,
        payload: dict[str, Any] | None,
        extra_conditions: tuple[ColumnElement[bool], ...],
        now: datetime,
    ) -> Workspace | None:
        """Claim the transition with one guarded write when row locks are unavailable."""
        values: dict[str, Any] = {
            "status": to.value,
            "updated_at": now,
            "event_sequence": Workspace.event_sequence + 1,
            "version": Workspace.version + 1,
        }
        if to == WorkspaceStatus.monitoring_pr:
            values["monitor_started_at"] = case(
                (Workspace.monitor_started_at.is_(None), now),
                else_=Workspace.monitor_started_at,
            )
        if _transition_releases_provisioning_execution_claim(from_status.value, to):
            values["execution_claimed_by"] = None
            values["execution_claim_expires_at"] = None

        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.status == from_status.value,
                *extra_conditions,
            )
            .values(**values)
            .returning(Workspace.event_sequence, Workspace.version)
            .execution_options(synchronize_session=False)
        )
        row = result.one_or_none()
        if row is None:
            return None

        event_order = cast(int, row[0])
        workspace = (
            await self._session.execute(
                select(Workspace)
                .where(Workspace.id == workspace_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if workspace is None:
            return None

        return await self._finish_transition_if_current(
            workspace,
            old_state=from_status.value,
            to=to,
            reason_code=reason_code,
            payload=payload,
            event_order=event_order,
            now=now,
        )

    async def _finish_transition_if_current(
        self,
        workspace: Workspace,
        *,
        old_state: str,
        to: WorkspaceStatus,
        reason_code: str,
        payload: dict[str, Any] | None,
        event_order: int,
        now: datetime,
    ) -> Workspace:
        attempt = await TaskAttemptRepository(
            self._session,
            dialect_name=self._dialect_name,
        ).get_by_workspace_id(workspace.id)
        if attempt is not None:
            attempt.status = to.value
        if to == WorkspaceStatus.monitoring_pr and workspace.monitor_started_at is None:
            workspace.monitor_started_at = now
        await self._sync_merge_candidate_lifecycle(workspace, attempt=attempt, to=to)
        if _releases_resource_reservation(to):
            await ResourceReservationRepository(self._session).release_active_for_workspace(
                workspace.id,
                released_at=now,
            )

        workspace.events.append(
            WorkspaceEvent(
                id=new_event_id(),
                event_type="workspace.state_changed",
                old_state=old_state,
                new_state=to.value,
                reason_code=reason_code,
                payload=payload,
                event_order=event_order,
            )
        )
        await self._session.flush()
        return workspace

    async def _sync_merge_candidate_lifecycle(
        self,
        workspace: Workspace,
        *,
        attempt: TaskAttempt | None,
        to: WorkspaceStatus,
    ) -> None:
        candidate_repo = MergeCandidateRepository(self._session)
        if to == WorkspaceStatus.monitoring_pr:
            if attempt is None or not workspace.pr_url:
                return
            task = await TaskRepository(self._session).get(attempt.task_id)
            if task is None:
                return
            await candidate_repo.make_attempt_canonical_and_create_candidate(
                task=task,
                attempt=attempt,
                workspace=workspace,
                head_sha=workspace.monitor_last_commit_sha,
                base_sha=workspace.base_commit,
            )
            return

        if to == WorkspaceStatus.completed:
            await candidate_repo.mark_workspace_merged(workspace.id)
            return

        if to in {WorkspaceStatus.failed, WorkspaceStatus.cancelled}:
            await candidate_repo.close_open_for_workspace(
                workspace.id,
                close_reason=_candidate_terminal_close_reason(to),
            )

    async def claim_monitoring_pr(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
        now: datetime | None = None,
        clear_stale_execution_claim_cutoff: datetime | None = None,
    ) -> bool:
        """Claim a monitor-recovery workspace unless another lease is active."""
        return await claim_monitoring_pr(
            self._session,
            workspace_id,
            owner_id=owner_id,
            lease_expires_at=lease_expires_at,
            now=now,
            clear_stale_execution_claim_cutoff=clear_stale_execution_claim_cutoff,
        )

    async def claim_worker_restart_recovery_execution(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
        claim_cutoff: datetime,
    ) -> Workspace | None:
        """Atomically adopt a worker-restart execution recovery lease."""
        return await claim_worker_restart_recovery_execution(
            self._session,
            workspace_id,
            owner_id=owner_id,
            lease_expires_at=lease_expires_at,
            claim_cutoff=claim_cutoff,
        )

    async def refresh_monitoring_pr_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> bool:
        """Extend this worker's active monitor-recovery lease."""
        return await refresh_monitoring_pr_claim(
            self._session,
            workspace_id,
            owner_id=owner_id,
            lease_expires_at=lease_expires_at,
        )

    async def refresh_execution_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
        execution_claim_epoch: int | None = None,
    ) -> bool:
        """Extend this worker's active-execution lease.

        When ``execution_claim_epoch`` is supplied (the provisioning fencing
        path), the update also requires the row's epoch to still match: a later
        claimant always holds a strictly higher epoch, so a stale worker's
        heartbeat updates 0 rows and the caller knows it has been fenced.
        ``None`` preserves the legacy owner-only behavior (the executor path).
        """
        return await refresh_execution_claim(
            self._session,
            workspace_id,
            owner_id=owner_id,
            lease_expires_at=lease_expires_at,
            execution_claim_epoch=execution_claim_epoch,
        )

    async def release_execution_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        execution_claim_epoch: int | None = None,
    ) -> bool:
        """Release this worker's active-execution lease, if it still owns it.

        When ``execution_claim_epoch`` is supplied, the release is gated on the
        epoch as well, so a release issued after a newer claimant has reclaimed
        the row (advancing the epoch) updates 0 rows instead of clobbering the
        new claimant's lease.
        """
        return await release_execution_claim(
            self._session,
            workspace_id,
            owner_id=owner_id,
            execution_claim_epoch=execution_claim_epoch,
        )

    async def read_execution_claim_epoch(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> int | None:
        """Return the current execution-claim epoch if ``owner_id`` still owns it.

        Returns ``None`` when the row is gone or the claim is no longer held by
        ``owner_id`` (e.g. a newer claimant reclaimed it). The worker reads its
        epoch back at provision start via this method and aborts when it is
        ``None`` (D2).
        """
        return await read_execution_claim_epoch(
            self._session,
            workspace_id,
            owner_id=owner_id,
        )

    async def release_monitoring_pr_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> bool:
        """Release this worker's monitor-recovery lease, if it still owns it."""
        return await release_monitoring_pr_claim(
            self._session,
            workspace_id,
            owner_id=owner_id,
        )

    async def add_event(
        self,
        workspace: Workspace,
        *,
        event_type: str,
        reason_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkspaceEvent:
        """Append one event to a workspace timeline."""
        events = await self.add_events(
            workspace,
            events=[
                WorkspaceEventCreate(
                    event_type=event_type,
                    reason_code=reason_code,
                    payload=payload,
                )
            ],
        )
        return events[0]

    async def add_event_with_states(
        self,
        workspace: Workspace,
        *,
        event_type: str,
        old_state: WorkspaceStatus | str,
        new_state: WorkspaceStatus | str,
        reason_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkspaceEvent:
        """Append one state-transition event to a workspace timeline."""
        event_order = await self._reserve_workspace_event_orders(workspace, count=1)
        event = WorkspaceEvent(
            id=new_event_id(),
            workspace_id=workspace.id,
            event_type=event_type,
            old_state=_workspace_status_value(old_state),
            new_state=_workspace_status_value(new_state),
            reason_code=reason_code,
            payload=payload,
            event_order=event_order,
        )
        workspace.events.append(event)
        await self._session.flush()
        return event

    async def add_audit_event(
        self,
        workspace: Workspace,
        *,
        event_type: str,
        actor: str,
        action: str,
        outcome: str,
        reason_code: str,
        source: str | None = None,
        operation_id: str | None = None,
        operation_type: str | None = None,
        pr_number: int | None = None,
        pr_url: str | None = None,
        source_head_sha: str | None = None,
        source_base_sha: str | None = None,
        target_branch: str | None = None,
        remote_branch: str | None = None,
        branch_name: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> WorkspaceEvent:
        """Append an audit-shaped workspace event."""
        return await self.add_event(
            workspace,
            event_type=event_type,
            reason_code=reason_code,
            payload=build_audit_payload(
                actor=actor,
                source=source,
                action=action,
                outcome=outcome,
                reason_code=reason_code,
                operation_id=operation_id,
                operation_type=operation_type,
                pr_number=pr_number,
                pr_url=pr_url,
                source_head_sha=source_head_sha,
                source_base_sha=source_base_sha,
                target_branch=target_branch,
                remote_branch=remote_branch,
                branch_name=branch_name,
                evidence=evidence,
                extra=extra,
            ),
        )

    async def record_ignored_stale_callback(
        self,
        workspace: Workspace,
        *,
        callback_source: str,
        callback_action: str,
        expected_status: WorkspaceStatus | str,
        requested_status: WorkspaceStatus | str | None = None,
        operation_id: str | None = None,
        reason_code: str | None = None,
    ) -> WorkspaceEvent:
        """Record that a stale asynchronous callback was ignored."""
        expected_status_value = (
            expected_status.value
            if isinstance(expected_status, WorkspaceStatus)
            else expected_status
        )
        payload: dict[str, Any] = {
            "callback_source": callback_source,
            "callback_action": callback_action,
            "expected_status": expected_status_value,
            "actual_status": workspace.status,
        }
        if requested_status is not None:
            payload["requested_status"] = (
                requested_status.value
                if isinstance(requested_status, WorkspaceStatus)
                else requested_status
            )
        if operation_id is not None:
            payload["operation_id"] = operation_id
        if reason_code is not None:
            payload["reason_code"] = reason_code
        return await self.add_event(
            workspace,
            event_type="workspace.stale_callback_ignored",
            reason_code="STALE_CALLBACK_IGNORED",
            payload=payload,
        )

    async def advance_workspace_version(self, workspace: Workspace) -> int:
        """Advance the optimistic-control version for a real workspace mutation.

        ``updated_at`` is intentionally preserved for this helper's atomic
        UPDATE. Callers that also mutate workspace content fields rely on their
        ORM writes and the Workspace ``onupdate`` hook to record the actual
        content-change timestamp.
        """
        result = await self._session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .values(
                version=Workspace.version + 1,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.version)
        )
        new_version = result.scalar_one()
        set_committed_value(workspace, "version", new_version)
        return new_version

    async def _reserve_workspace_event_orders(
        self,
        workspace: Workspace,
        *,
        count: int,
        bump_version: bool = False,
    ) -> int:
        values: dict[str, Any] = {
            "event_sequence": Workspace.event_sequence + count,
            "updated_at": Workspace.updated_at,
        }
        if bump_version:
            values["version"] = Workspace.version + 1
        result = await self._session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .values(**values)
            .returning(Workspace.event_sequence, Workspace.version)
        )
        row = result.one()
        new_event_sequence = cast(int, row[0])
        set_committed_value(workspace, "event_sequence", new_event_sequence)
        if bump_version:
            new_version = cast(int, row[1])
            set_committed_value(workspace, "version", new_version)
        return new_event_sequence - count + 1

    async def add_events(
        self,
        workspace: Workspace,
        *,
        events: builtins.list[WorkspaceEventCreate],
    ) -> builtins.list[WorkspaceEvent]:
        """Append events and reserve workspace-local event_order values."""
        if not events:
            return []

        first_event_order = await self._reserve_workspace_event_orders(
            workspace,
            count=len(events),
        )
        created = [
            WorkspaceEvent(
                id=new_event_id(),
                workspace_id=workspace.id,
                event_type=event.event_type,
                old_state=workspace.status,
                new_state=workspace.status,
                reason_code=event.reason_code,
                payload=event.payload,
                event_order=first_event_order + index,
            )
            for index, event in enumerate(events)
        ]
        workspace.events.extend(created)
        await self._session.flush()
        return created


from awf.db.repositories.workspace_repo_callbacks import (  # noqa: E402
    CallbackDeliveryRepository,
    CallbackIdempotencyConflictError,
    CallbackSubscriptionRepository,
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceLogStreamRepository,
)
from awf.db.repositories.workspace_repo_secrets import SecretLeaseRepository  # noqa: E402

__all__ = [
    "CallbackDeliveryRepository",
    "CallbackIdempotencyConflictError",
    "CallbackSubscriptionRepository",
    "OperationRepository",
    "SecretLeaseRepository",
    "WorkspaceEventRepository",
    "WorkspaceLogStreamRepository",
    "WorkspaceRepository",
    "new_workspace_id",
    "set_committed_value",
]
