"""Workspace, WorkspaceEvent, Operation, LogStream, and SecretLease database repositories."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    and_,
    case,
    func,
    literal,
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
    new_callback_delivery_id,
    new_callback_subscription_id,
    new_event_id,
    new_log_stream_id,
    new_operation_id,
    new_secret_lease_id,
)
from awf.db.enums import (
    AgentRuntime,
    CallbackDeliveryStatus,
    CallbackEventKind,
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    MergeCandidate,
    Operation,
    TaskAttempt,
    Workspace,
    WorkspaceEvent,
    WorkspaceLogStream,
    WorkspaceSecretLease,
)
from awf.db.repositories.base import (
    _SECRET_LEASE_ACTIVE_STATUSES,
    _SECRET_LEASE_REVOCABLE_STATUSES,
    ACTIVE_OWNED_PATH_OVERLAP_STATUSES,
    DEFAULT_IDEMPOTENCY_REPLAY_KEY_LIMIT,
    SECRET_LEASE_AUDIT_EVENT_TYPE,
    SECRET_LEASE_STATUS_EXPIRED,
    SECRET_LEASE_STATUS_ISSUED,
    SECRET_LEASE_STATUS_MOUNTED,
    SECRET_LEASE_STATUS_REVOKED,
    OwnedPathConflict,
    OwnedPathOverlap,
    SecretLeaseIssue,
    WorkspaceEventCreate,
    _callback_delivery_insert_if_absent_stmt,
    _callback_subscription_event_type_candidates,
    _callback_subscription_event_type_filter,
    _callback_subscription_idempotency_advisory_lock_key,
    _callback_subscription_insert_if_absent_stmt,
    _candidate_terminal_close_reason,
    _declared_lease_requires_reissue,
    _group_leases_by_workspace,
    _IssuedSecretLease,
    _lease_audit_payload,
    _matches_pr_adoption_identity,
    _normalize_owned_path,
    _operation_idempotency_advisory_lock_key,
    _operation_result_with_log_stream_refs,
    _owned_path_conflict_advisory_lock_key,
    _owned_paths_overlap,
    _reissue_declared_lease,
    _releases_resource_reservation,
    _sanitize_metadata,
    _schedulable_workspace_ids_stmt,
    _scheduler_scoring_time,
    _secret_lease_declaration_key,
    _secret_lease_insert_if_absent_stmt,
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
    _repo_proxy.set_committed_value(instance, key, value)


def new_workspace_id() -> str:
    return cast(str, _repo_proxy.new_workspace_id())


class CallbackIdempotencyConflictError(ValueError):
    """Raised when an idempotency key is replayed with a different request body."""


class SecretLeaseRepository:
    """CRUD helpers for local workspace secret lease metadata."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def issue_declared_leases(
        self,
        workspace: Workspace,
        *,
        leases: Iterable[SecretLeaseIssue],
        now: datetime,
    ) -> list[WorkspaceSecretLease]:
        issues = list(leases)
        if not issues:
            return []

        existing_by_declaration = await self._leases_by_declaration_for_workspace(workspace.id)
        issue_events: list[WorkspaceSecretLease] = []
        results: list[WorkspaceSecretLease] = []
        for issue in issues:
            declaration_key = _secret_lease_declaration_key(
                issue.secret_name,
                issue.kind,
                issue.target,
            )
            existing = existing_by_declaration.get(declaration_key)
            if existing is not None:
                if _declared_lease_requires_reissue(existing, issue):
                    _reissue_declared_lease(existing, issue=issue, now=now)
                    issue_events.append(existing)
                results.append(existing)
                continue

            issued = await self._issue_declared_lease_if_absent(
                workspace,
                issue=issue,
                now=now,
            )
            existing_by_declaration[declaration_key] = issued.lease
            results.append(issued.lease)
            if issued.issue_event_required:
                issue_events.append(issued.lease)
        if issue_events:
            await self._add_lease_events(
                workspace,
                leases=issue_events,
                reason_code="SECRET_LEASE_ISSUED",
                action="issue",
                now=now,
            )
        return results

    async def _issue_declared_lease_if_absent(
        self,
        workspace: Workspace,
        *,
        issue: SecretLeaseIssue,
        now: datetime,
    ) -> _IssuedSecretLease:
        values = {
            "id": new_secret_lease_id(),
            "workspace_id": workspace.id,
            "attempt_id": issue.attempt_id,
            "secret_name": issue.secret_name,
            "kind": issue.kind,
            "target": issue.target,
            "mode": issue.mode,
            "required": issue.required,
            "provider": issue.provider,
            "ref_digest": issue.ref_digest,
            "status": SECRET_LEASE_STATUS_ISSUED,
            "issued_at": now,
            "expires_at": issue.expires_at,
            "issue_metadata": _sanitize_metadata(issue.issue_metadata),
            "mount_metadata": {},
        }
        conflict_guarded, inserted_id = await self._insert_declared_lease_if_absent(values)
        if inserted_id is not None:
            lease = await self._session.get(WorkspaceSecretLease, inserted_id)
            if lease is None:
                raise RuntimeError(f"inserted secret lease {inserted_id} was not visible")
            set_committed_value(lease, "issued_at", now)
            set_committed_value(lease, "expires_at", issue.expires_at)
            return _IssuedSecretLease(lease=lease, issue_event_required=True)

        existing = await self._get_for_declaration(
            workspace.id,
            secret_name=issue.secret_name,
            kind=issue.kind,
            target=issue.target,
        )
        if existing is not None:
            if _declared_lease_requires_reissue(existing, issue):
                _reissue_declared_lease(existing, issue=issue, now=now)
                return _IssuedSecretLease(lease=existing, issue_event_required=True)
            return _IssuedSecretLease(lease=existing, issue_event_required=False)
        if conflict_guarded:
            raise RuntimeError(
                "secret lease insert hit a declaration conflict but no existing row was visible"
            )

        lease = WorkspaceSecretLease(**values)
        self._session.add(lease)
        await self._session.flush()
        return _IssuedSecretLease(lease=lease, issue_event_required=True)

    async def _insert_declared_lease_if_absent(
        self,
        values: Mapping[str, Any],
    ) -> tuple[bool, str | None]:
        stmt = _secret_lease_insert_if_absent_stmt(self._dialect_name)
        if stmt is None:
            return False, None
        result = await self._session.execute(stmt.values(**values))
        return True, result.scalar_one_or_none()

    async def mark_issued_mounted(
        self,
        workspace: Workspace,
        *,
        now: datetime,
        mount_metadata: Mapping[str, Any] | None = None,
    ) -> list[WorkspaceSecretLease]:
        leases = await self._list_for_workspace_statuses(
            workspace.id,
            statuses=(SECRET_LEASE_STATUS_ISSUED,),
        )
        sanitized_metadata = _sanitize_metadata(dict(mount_metadata or {}))
        for lease in leases:
            lease.status = SECRET_LEASE_STATUS_MOUNTED
            lease.mounted_at = now
            lease.mount_metadata = sanitized_metadata
        await self._session.flush()
        if leases:
            await self._add_lease_events(
                workspace,
                leases=leases,
                reason_code="SECRET_LEASE_MOUNTED",
                action="mount",
                now=now,
            )
        return leases

    async def expire_due_leases(self, *, now: datetime) -> list[WorkspaceSecretLease]:
        stmt = (
            select(WorkspaceSecretLease)
            .where(
                WorkspaceSecretLease.status.in_(_SECRET_LEASE_ACTIVE_STATUSES),
                WorkspaceSecretLease.expires_at.is_not(None),
                WorkspaceSecretLease.expires_at <= now,
            )
            .order_by(WorkspaceSecretLease.workspace_id, WorkspaceSecretLease.issued_at)
        )
        leases = list((await self._session.execute(stmt)).scalars())
        if not leases:
            return []
        workspaces = await self._workspaces_by_id({lease.workspace_id for lease in leases})
        for lease in leases:
            lease.status = SECRET_LEASE_STATUS_EXPIRED
        await self._session.flush()
        for workspace_id, workspace_leases in _group_leases_by_workspace(leases).items():
            workspace = workspaces.get(workspace_id)
            if workspace is None:
                continue
            await self._add_lease_events(
                workspace,
                leases=workspace_leases,
                reason_code="SECRET_LEASE_EXPIRED",
                action="expire",
                now=now,
            )
        return leases

    async def revoke_workspace_leases(
        self,
        workspace: Workspace,
        *,
        now: datetime,
        reason_code: str,
    ) -> list[WorkspaceSecretLease]:
        leases = await self._list_for_workspace_statuses(
            workspace.id,
            statuses=_SECRET_LEASE_REVOCABLE_STATUSES,
        )
        for lease in leases:
            lease.status = SECRET_LEASE_STATUS_REVOKED
            lease.revoked_at = now
            lease.revoke_reason_code = reason_code
        await self._session.flush()
        if leases:
            await self._add_lease_events(
                workspace,
                leases=leases,
                reason_code="SECRET_LEASE_REVOKED",
                action="revoke",
                now=now,
            )
        return leases

    async def list_for_workspace(self, workspace_id: str) -> list[WorkspaceSecretLease]:
        stmt = (
            select(WorkspaceSecretLease)
            .where(WorkspaceSecretLease.workspace_id == workspace_id)
            .order_by(WorkspaceSecretLease.issued_at, WorkspaceSecretLease.id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def _get_for_declaration(
        self,
        workspace_id: str,
        *,
        secret_name: str,
        kind: str,
        target: str,
    ) -> WorkspaceSecretLease | None:
        stmt = select(WorkspaceSecretLease).where(
            WorkspaceSecretLease.workspace_id == workspace_id,
            WorkspaceSecretLease.secret_name == secret_name,
            WorkspaceSecretLease.kind == kind,
            WorkspaceSecretLease.target == target,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _leases_by_declaration_for_workspace(
        self,
        workspace_id: str,
    ) -> dict[tuple[str, str, str], WorkspaceSecretLease]:
        stmt = select(WorkspaceSecretLease).where(WorkspaceSecretLease.workspace_id == workspace_id)
        rows = (await self._session.execute(stmt)).scalars()
        return {
            _secret_lease_declaration_key(lease.secret_name, lease.kind, lease.target): lease
            for lease in rows
        }

    async def _list_for_workspace_statuses(
        self,
        workspace_id: str,
        *,
        statuses: tuple[str, ...],
    ) -> list[WorkspaceSecretLease]:
        stmt = (
            select(WorkspaceSecretLease)
            .where(
                WorkspaceSecretLease.workspace_id == workspace_id,
                WorkspaceSecretLease.status.in_(statuses),
            )
            .order_by(WorkspaceSecretLease.issued_at, WorkspaceSecretLease.id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def _workspaces_by_id(self, workspace_ids: set[str]) -> dict[str, Workspace]:
        if not workspace_ids:
            return {}
        stmt = select(Workspace).where(Workspace.id.in_(workspace_ids))
        rows = (await self._session.execute(stmt)).scalars()
        return {workspace.id: workspace for workspace in rows}

    async def _add_lease_events(
        self,
        workspace: Workspace,
        *,
        leases: list[WorkspaceSecretLease],
        reason_code: str,
        action: str,
        now: datetime,
    ) -> None:
        events = [
            WorkspaceEventCreate(
                event_type=SECRET_LEASE_AUDIT_EVENT_TYPE,
                reason_code=reason_code,
                payload=_lease_audit_payload(
                    lease,
                    action=action,
                    reason_code=reason_code,
                    occurred_at=now,
                ),
            )
            for lease in leases
        ]
        await WorkspaceRepository(self._session).add_events(workspace, events=events)


class WorkspaceRepository:
    """CRUD + state transitions for workspaces.

    Holds a reference to an ``AsyncSession`` for the life of one logical unit
    of work. Do not reuse across request boundaries.
    """

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    @property
    def dialect_name(self) -> str | None:
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
    ) -> Workspace:
        """Create a new workspace in ``requested`` status and emit a creation event.

        Does not commit — the caller owns the transaction boundary.
        """
        workspace = Workspace(
            id=new_workspace_id(),
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
        return await self._session.get(Workspace, workspace_id)

    async def get_with_secret_leases(self, workspace_id: str) -> Workspace | None:
        stmt = (
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .options(selectinload(Workspace.secret_leases))
            .options(selectinload(Workspace.operations))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_with_operations(self, workspace_id: str) -> Workspace | None:
        stmt = (
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .options(selectinload(Workspace.operations))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_with_validation_runs(self, workspace_id: str) -> Workspace | None:
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
        stmt = select(Workspace.id).where(Workspace.id == workspace_id).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def get_by_idempotency_key(self, key: str) -> Workspace | None:
        stmt = (
            select(Workspace)
            .where(Workspace.idempotency_key == key)
            .options(selectinload(Workspace.task_attempt))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def has_idempotency_key(self, key: str) -> bool:
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
        """Serialize owned-path admission for one repo/base transaction on Postgres."""
        if not any(_normalize_owned_path(path) != "" for path in owned_paths):
            return

        if self._dialect_name != "postgresql":
            return

        lock_key = _owned_path_conflict_advisory_lock_key(
            repo_url=repo_url,
            branch_base=branch_base,
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def find_active_owned_path_conflicts(
        self,
        *,
        repo_url: str,
        branch_base: str,
        owned_paths: list[str],
    ) -> list[OwnedPathConflict]:
        overlaps = await self.find_active_owned_path_overlaps(
            repo_url=repo_url,
            branch_base=branch_base,
            owned_paths=owned_paths,
        )
        return [
            OwnedPathConflict(
                workspace_id=overlap.workspace_id,
                existing_path=overlap.existing_path,
                requested_path=overlap.requested_path,
            )
            for overlap in overlaps
        ]

    async def find_active_owned_path_overlaps(
        self,
        *,
        repo_url: str,
        branch_base: str,
        owned_paths: list[str],
    ) -> list[OwnedPathOverlap]:
        requested_paths = [path for path in owned_paths if _normalize_owned_path(path) != ""]
        if not requested_paths:
            return []

        stmt = (
            select(Workspace)
            .where(
                Workspace.repo_url == repo_url,
                Workspace.branch_base == branch_base,
                Workspace.status.in_(ACTIVE_OWNED_PATH_OVERLAP_STATUSES),
            )
            .order_by(Workspace.created_at.asc(), Workspace.id.asc())
        )
        rows = list((await self._session.execute(stmt)).scalars())
        overlaps: list[OwnedPathOverlap] = []
        for workspace in rows:
            for existing_path in workspace.owned_paths:
                if _normalize_owned_path(existing_path) == "":
                    continue
                for requested_path in requested_paths:
                    if _owned_paths_overlap(existing_path, requested_path):
                        overlaps.append(
                            OwnedPathOverlap(
                                workspace_id=workspace.id,
                                existing_path=existing_path,
                                requested_path=requested_path,
                            )
                        )
        return overlaps

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
        cutoff = now or datetime.now(UTC)
        values: dict[str, Any] = {
            "monitor_claimed_by": owner_id,
            "monitor_claim_expires_at": lease_expires_at,
            "updated_at": Workspace.updated_at,
        }
        if clear_stale_execution_claim_cutoff is not None:
            stale_execution_claim = or_(
                Workspace.execution_claimed_by.is_(None),
                Workspace.execution_claim_expires_at.is_(None),
                Workspace.execution_claim_expires_at <= clear_stale_execution_claim_cutoff,
            )
            values.update(
                execution_claimed_by=case(
                    (stale_execution_claim, None),
                    else_=Workspace.execution_claimed_by,
                ),
                execution_claim_expires_at=case(
                    (stale_execution_claim, None),
                    else_=Workspace.execution_claim_expires_at,
                ),
            )
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.status == WorkspaceStatus.monitoring_pr.value,
                or_(
                    Workspace.monitor_claim_expires_at.is_(None),
                    Workspace.monitor_claim_expires_at <= cutoff,
                    Workspace.monitor_claimed_by == owner_id,
                ),
            )
            .values(**values)
            .returning(Workspace.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None

    async def claim_worker_restart_recovery_execution(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
        claim_cutoff: datetime,
    ) -> Workspace | None:
        """Atomically adopt a worker-restart execution recovery lease."""
        from awf.db.repositories.base import (
            _ACTIVE_RECOVERY_OPERATION_STATUSES,
            _VALIDATE_ONLY_RECOVERY_MODES,
            _WORKER_RESTART_RECOVERY_EXECUTION_CLAIM_STATUSES,
        )

        active_worker_restart_recovery = (
            select(literal(1))
            .select_from(Operation)
            .where(
                Operation.workspace_id == workspace_id,
                Operation.status.in_(_ACTIVE_RECOVERY_OPERATION_STATUSES),
                Operation.payload["source"].as_string() == "worker_restart",
                or_(
                    and_(
                        Operation.type == OperationType.validate.value,
                        Operation.payload["recovery_mode"]
                        .as_string()
                        .in_(_VALIDATE_ONLY_RECOVERY_MODES),
                    ),
                    and_(
                        Operation.type == OperationType.rebase.value,
                        Operation.payload["recovery_mode"].as_string() == "rebase_only",
                    ),
                ),
            )
            .exists()
        )
        claim_available = or_(
            Workspace.execution_claimed_by.is_(None),
            Workspace.execution_claim_expires_at.is_(None),
            Workspace.execution_claim_expires_at <= claim_cutoff,
            Workspace.execution_claimed_by == owner_id,
        )
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.status.in_(_WORKER_RESTART_RECOVERY_EXECUTION_CLAIM_STATUSES),
                active_worker_restart_recovery,
                claim_available,
            )
            .values(
                execution_claimed_by=owner_id,
                execution_claim_expires_at=lease_expires_at,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.id)
            .execution_options(synchronize_session=False)
        )
        if result.scalar_one_or_none() is None:
            return None

        stmt = (
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .options(selectinload(Workspace.operations))
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def refresh_monitoring_pr_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> bool:
        """Extend this worker's active monitor-recovery lease."""
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.status == WorkspaceStatus.monitoring_pr.value,
                Workspace.monitor_claimed_by == owner_id,
            )
            .values(
                monitor_claim_expires_at=lease_expires_at,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.id)
        )
        return result.scalar_one_or_none() is not None

    async def refresh_execution_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> bool:
        """Extend this worker's active-execution lease."""
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.execution_claimed_by == owner_id,
            )
            .values(
                execution_claim_expires_at=lease_expires_at,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.id)
        )
        return result.scalar_one_or_none() is not None

    async def release_execution_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> bool:
        """Release this worker's active-execution lease, if it still owns it."""
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.execution_claimed_by == owner_id,
            )
            .values(
                execution_claimed_by=None,
                execution_claim_expires_at=None,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.id)
        )
        return result.scalar_one_or_none() is not None

    async def release_monitoring_pr_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> bool:
        """Release this worker's monitor-recovery lease, if it still owns it."""
        result = await self._session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.monitor_claimed_by == owner_id,
            )
            .values(
                monitor_claimed_by=None,
                monitor_claim_expires_at=None,
                updated_at=Workspace.updated_at,
            )
            .returning(Workspace.id)
        )
        return result.scalar_one_or_none() is not None

    async def add_event(
        self,
        workspace: Workspace,
        *,
        event_type: str,
        reason_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkspaceEvent:
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


class WorkspaceEventRepository:
    """Read-only queries for immutable workspace events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(
        self,
        *,
        workspace_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[WorkspaceEvent]:
        stmt = select(WorkspaceEvent)
        if workspace_id is not None:
            stmt = stmt.where(WorkspaceEvent.workspace_id == workspace_id)
        if event_type is not None:
            stmt = stmt.where(WorkspaceEvent.event_type == event_type)
        stmt = stmt.order_by(
            WorkspaceEvent.occurred_at.desc(),
            WorkspaceEvent.id.desc(),
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars())


class CallbackSubscriptionRepository:
    """CRUD helpers for external callback registrations."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def acquire_idempotency_key_lock(self, key: str) -> None:
        """Serialize callback subscription idempotency decisions."""
        lock_key = _callback_subscription_idempotency_advisory_lock_key(key)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def create_idempotent(
        self,
        *,
        name: str,
        target_url: str,
        event_types: list[str],
        enabled: bool,
        timeout_seconds: int,
        max_attempts: int,
        initial_backoff_seconds: int,
        idempotency_key: str,
        request_hash: str,
        acquire_lock: bool = True,
    ) -> tuple[CallbackSubscription, bool]:
        if acquire_lock:
            await self.acquire_idempotency_key_lock(idempotency_key)
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise CallbackIdempotencyConflictError(
                    "Idempotency-Key previously used with a different callback request."
                )
            return existing, False

        now = datetime.now(UTC)
        subscription_values: dict[str, Any] = {
            "id": new_callback_subscription_id(),
            "name": name,
            "target_url": target_url,
            "event_types": list(event_types),
            "enabled": enabled,
            "timeout_seconds": timeout_seconds,
            "max_attempts": max_attempts,
            "initial_backoff_seconds": initial_backoff_seconds,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "created_at": now,
            "updated_at": now,
            "disabled_at": None if enabled else now,
        }
        insert_if_absent = _callback_subscription_insert_if_absent_stmt(self._dialect_name)
        if insert_if_absent is not None:
            result = await self._session.execute(insert_if_absent.values(**subscription_values))
            inserted_id = result.scalar_one_or_none()
            if inserted_id is not None:
                inserted = await self.get(inserted_id)
                if inserted is None:
                    raise RuntimeError("Inserted callback subscription could not be loaded.")
                return inserted, True

            existing = await self.get_by_idempotency_key(idempotency_key)
            if existing is None:
                raise RuntimeError(
                    "Callback subscription insert conflicted but no row could be loaded."
                )
            if existing.request_hash != request_hash:
                raise CallbackIdempotencyConflictError(
                    "Idempotency-Key previously used with a different callback request."
                )
            return existing, False

        subscription = CallbackSubscription(**subscription_values)
        self._session.add(subscription)
        await self._session.flush()
        return subscription, True

    async def get(self, subscription_id: str) -> CallbackSubscription | None:
        return await self._session.get(CallbackSubscription, subscription_id)

    async def get_by_idempotency_key(self, key: str) -> CallbackSubscription | None:
        stmt = select(CallbackSubscription).where(CallbackSubscription.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_idempotency_request_hash(self, key: str) -> str | None:
        stmt = select(CallbackSubscription.request_hash).where(
            CallbackSubscription.idempotency_key == key
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_idempotency_replay_keys(
        self,
        *,
        limit: int = DEFAULT_IDEMPOTENCY_REPLAY_KEY_LIMIT,
    ) -> builtins.list[tuple[str, str]]:
        """Return bounded callback replay keys for non-request-path cache support."""
        if limit <= 0:
            return []
        stmt = (
            select(CallbackSubscription.idempotency_key, CallbackSubscription.request_hash)
            .where(
                CallbackSubscription.idempotency_key.is_not(None),
                CallbackSubscription.request_hash.is_not(None),
            )
            .order_by(
                CallbackSubscription.created_at.asc(),
                CallbackSubscription.id.asc(),
            )
            .limit(limit)
        )
        return [(key, request_hash) for key, request_hash in (await self._session.execute(stmt))]

    async def list(
        self,
        *,
        enabled: bool | None = None,
        limit: int = 50,
    ) -> builtins.list[CallbackSubscription]:
        stmt = select(CallbackSubscription)
        if enabled is not None:
            stmt = stmt.where(CallbackSubscription.enabled.is_(enabled))
        stmt = stmt.order_by(
            CallbackSubscription.created_at.desc(),
            CallbackSubscription.id.desc(),
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def list_enabled_for_event_type(
        self,
        event_type: str,
    ) -> builtins.list[CallbackSubscription]:
        event_type_candidates = _callback_subscription_event_type_candidates(event_type)
        if not event_type_candidates:
            return []

        stmt = (
            select(CallbackSubscription)
            .where(
                CallbackSubscription.enabled.is_(True),
                _callback_subscription_event_type_filter(
                    event_type_candidates,
                    self._dialect_name,
                ),
            )
            .order_by(CallbackSubscription.created_at.asc(), CallbackSubscription.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars())


class CallbackDeliveryRepository:
    """CRUD helpers for durable callback delivery records."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def get(self, delivery_id: str) -> CallbackDelivery | None:
        stmt = (
            select(CallbackDelivery)
            .where(CallbackDelivery.id == delivery_id)
            .options(selectinload(CallbackDelivery.subscription))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def enqueue_once(
        self,
        *,
        subscription: CallbackSubscription,
        event_kind: CallbackEventKind | str,
        event_type: str,
        source_id: str,
        dedupe_key: str,
        workspace_id: str | None,
        operation_id: str | None,
        merge_candidate_id: str | None,
        envelope: dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[CallbackDelivery, bool]:
        existing = await self.get_by_dedupe_key(
            subscription_id=subscription.id,
            dedupe_key=dedupe_key,
        )
        if existing is not None:
            return existing, False

        created_at = now or datetime.now(UTC)
        delivery_id = new_callback_delivery_id()
        event_kind_value = (
            event_kind.value if isinstance(event_kind, CallbackEventKind) else event_kind
        )
        idempotency_key = f"callback-delivery:{subscription.id}:{dedupe_key}"
        delivery_envelope = dict(envelope)
        delivery_envelope["delivery"] = {
            "id": delivery_id,
            "subscription_id": subscription.id,
            "idempotency_key": idempotency_key,
            "dedupe_key": dedupe_key,
            "attempt_count": 0,
            "max_attempts": subscription.max_attempts,
        }
        delivery_values: dict[str, Any] = {
            "id": delivery_id,
            "subscription_id": subscription.id,
            "event_kind": event_kind_value,
            "event_type": event_type,
            "source_id": source_id,
            "dedupe_key": dedupe_key,
            "workspace_id": workspace_id,
            "operation_id": operation_id,
            "merge_candidate_id": merge_candidate_id,
            "envelope": delivery_envelope,
            "idempotency_key": idempotency_key,
            "status": CallbackDeliveryStatus.pending.value,
            "attempt_count": 0,
            "max_attempts": subscription.max_attempts,
            "next_attempt_at": created_at,
        }
        insert_if_absent = _callback_delivery_insert_if_absent_stmt(self._dialect_name)
        if insert_if_absent is not None:
            result = await self._session.execute(insert_if_absent.values(**delivery_values))
            inserted_id = result.scalar_one_or_none()
            if inserted_id is not None:
                inserted = await self.get(inserted_id)
                if inserted is None:
                    raise RuntimeError("Inserted callback delivery could not be loaded.")
                return inserted, True

            existing = await self.get_by_dedupe_key(
                subscription_id=subscription.id,
                dedupe_key=dedupe_key,
            )
            if existing is None:
                raise RuntimeError(
                    "Callback delivery insert conflicted but no row could be loaded."
                )
            return existing, False

        delivery = CallbackDelivery(**delivery_values)
        self._session.add(delivery)
        await self._session.flush()
        return delivery, True

    async def get_by_dedupe_key(
        self,
        *,
        subscription_id: str,
        dedupe_key: str,
    ) -> CallbackDelivery | None:
        stmt = select(CallbackDelivery).where(
            CallbackDelivery.subscription_id == subscription_id,
            CallbackDelivery.dedupe_key == dedupe_key,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[CallbackDelivery]:
        due_at = now or datetime.now(UTC)
        stmt = (
            select(CallbackDelivery)
            .where(
                CallbackDelivery.status == CallbackDeliveryStatus.pending.value,
                or_(
                    CallbackDelivery.next_attempt_at.is_(None),
                    CallbackDelivery.next_attempt_at <= due_at,
                ),
            )
            .options(selectinload(CallbackDelivery.subscription))
            .order_by(CallbackDelivery.created_at.asc(), CallbackDelivery.id.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def mark_attempt_started(
        self,
        delivery: CallbackDelivery,
        *,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        delivery.status = CallbackDeliveryStatus.running.value
        delivery.attempt_count += 1
        delivery.last_attempt_at = now or datetime.now(UTC)
        delivery.next_attempt_at = None
        delivery.response_status_code = None
        delivery.error_code = None
        delivery.error_message = None
        await self._session.flush()
        return delivery

    async def sync_envelope_delivery_metadata(
        self,
        delivery: CallbackDelivery,
    ) -> CallbackDelivery:
        envelope = dict(delivery.envelope)
        delivery_metadata = dict(envelope.get("delivery", {}))
        delivery_metadata.update(
            {
                "id": delivery.id,
                "subscription_id": delivery.subscription_id,
                "idempotency_key": delivery.idempotency_key,
                "dedupe_key": delivery.dedupe_key,
                "attempt_count": delivery.attempt_count,
                "max_attempts": delivery.max_attempts,
            }
        )
        envelope["delivery"] = delivery_metadata
        delivery.envelope = envelope
        await self._session.flush()
        return delivery

    async def mark_succeeded(
        self,
        delivery: CallbackDelivery,
        *,
        response_status_code: int,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        delivered_at = now or datetime.now(UTC)
        delivery.status = CallbackDeliveryStatus.succeeded.value
        delivery.delivered_at = delivered_at
        delivery.next_attempt_at = None
        delivery.response_status_code = response_status_code
        delivery.error_code = None
        delivery.error_message = None
        await self._session.flush()
        return delivery

    async def mark_failed_or_retry(
        self,
        delivery: CallbackDelivery,
        *,
        error_code: str,
        error_message: str,
        response_status_code: int | None,
        backoff_seconds: int,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        attempted_at = now or datetime.now(UTC)
        delivery.response_status_code = response_status_code
        delivery.error_code = error_code
        delivery.error_message = error_message[:512]
        if delivery.attempt_count >= delivery.max_attempts:
            delivery.status = CallbackDeliveryStatus.failed.value
            delivery.next_attempt_at = None
        else:
            delivery.status = CallbackDeliveryStatus.pending.value
            delivery.next_attempt_at = attempted_at + timedelta(seconds=backoff_seconds)
        await self._session.flush()
        return delivery

    async def mark_skipped(
        self,
        delivery: CallbackDelivery,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        skipped_at = now or datetime.now(UTC)
        delivery.status = CallbackDeliveryStatus.skipped.value
        delivery.last_attempt_at = skipped_at
        delivery.next_attempt_at = None
        delivery.error_code = error_code
        delivery.error_message = error_message[:512]
        await self._session.flush()
        return delivery


class OperationRepository:
    """CRUD helpers for async control-plane operations."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def acquire_idempotency_key_lock(self, key: str) -> None:
        """Serialize operation idempotency decisions with a PostgreSQL advisory lock."""
        lock_key = _operation_idempotency_advisory_lock_key(key)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def create(
        self,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        status: OperationStatus | str = OperationStatus.pending,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        status_value = status.value if isinstance(status, OperationStatus) else status
        operation = Operation(
            id=new_operation_id(),
            workspace_id=workspace_id,
            type=operation_type.value
            if isinstance(operation_type, OperationType)
            else operation_type,
            status=status_value,
            payload=payload,
            idempotency_key=idempotency_key,
            started_at=datetime.now(UTC) if status_value == OperationStatus.running.value else None,
        )
        self._session.add(operation)
        await self._session.flush()
        return operation

    async def create_idempotent(
        self,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        status: OperationStatus | str = OperationStatus.pending,
        payload: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> tuple[Operation, bool]:
        await self.acquire_idempotency_key_lock(idempotency_key)
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing, False
        return (
            await self.create(
                workspace_id=workspace_id,
                operation_type=operation_type,
                status=status,
                payload=payload,
                idempotency_key=idempotency_key,
            ),
            True,
        )

    async def get(self, operation_id: str) -> Operation | None:
        return await self._session.get(Operation, operation_id)

    async def start(self, operation: Operation) -> Operation:
        operation.status = OperationStatus.running.value
        if operation.started_at is None:
            operation.started_at = datetime.now(UTC)
        await self._session.flush()
        return operation

    async def get_by_idempotency_key(self, key: str) -> Operation | None:
        stmt = (
            select(Operation)
            .where(Operation.idempotency_key == key)
            .order_by(Operation.created_at.asc(), Operation.id.asc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_active_matching_payload(
        self,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        payload_identity: Mapping[str, Any],
        limit: int = 100,
    ) -> Operation | None:
        operation_type_value = (
            operation_type.value if isinstance(operation_type, OperationType) else operation_type
        )
        stmt = (
            select(Operation)
            .where(
                Operation.workspace_id == workspace_id,
                Operation.type == operation_type_value,
                Operation.status.in_(
                    (
                        OperationStatus.pending.value,
                        OperationStatus.running.value,
                    )
                ),
            )
            .order_by(Operation.created_at.asc(), Operation.id.asc())
            .limit(limit)
        )
        for operation in (await self._session.execute(stmt)).scalars():
            payload = operation.payload
            if not isinstance(payload, dict):
                continue
            if all(
                key in payload and payload[key] == value for key, value in payload_identity.items()
            ):
                return operation
        return None

    async def list_all(
        self,
        *,
        workspace_id: str | None = None,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
        before_created_at: datetime | None = None,
        before_operation_id: str | None = None,
    ) -> list[Operation]:
        stmt = select(Operation)
        status_value = status.value if isinstance(status, OperationStatus) else status
        operation_type_value = (
            operation_type.value if isinstance(operation_type, OperationType) else operation_type
        )
        if workspace_id is not None:
            stmt = stmt.where(Operation.workspace_id == workspace_id)
        if status_value is not None:
            stmt = stmt.where(Operation.status == status_value)
        if operation_type_value is not None:
            stmt = stmt.where(Operation.type == operation_type_value)
        if before_created_at is not None and before_operation_id is not None:
            stmt = stmt.where(
                or_(
                    Operation.created_at < before_created_at,
                    and_(
                        Operation.created_at == before_created_at,
                        Operation.id < before_operation_id,
                    ),
                )
            )

        stmt = stmt.order_by(Operation.created_at.desc(), Operation.id.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def list_for_workspace(
        self,
        workspace_id: str,
        *,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
        before_created_at: datetime | None = None,
        before_operation_id: str | None = None,
    ) -> list[Operation]:
        return await self.list_all(
            workspace_id=workspace_id,
            status=status,
            operation_type=operation_type,
            limit=limit,
            before_created_at=before_created_at,
            before_operation_id=before_operation_id,
        )

    async def finish(
        self,
        operation: Operation,
        *,
        status: OperationStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_stream_refs: Mapping[str, Any] | None = None,
    ) -> Operation:
        operation.status = status.value
        operation.result = _operation_result_with_log_stream_refs(
            result,
            log_stream_refs=log_stream_refs,
        )
        operation.error_code = error_code
        operation.error_message = error_message
        operation.finished_at = datetime.now(UTC)
        if operation.started_at is None:
            operation.started_at = operation.finished_at
        await self._session.flush()
        return operation


class WorkspaceLogStreamRepository:
    """Metadata index for durable workspace log streams."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_or_get(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        source: str,
        name: str,
        kind: str,
        path: str,
    ) -> WorkspaceLogStream:
        existing = await self.get(workspace_id=workspace_id, stream_id=stream_id)
        if existing is not None:
            return existing
        stream = WorkspaceLogStream(
            id=new_log_stream_id(),
            workspace_id=workspace_id,
            stream_id=stream_id,
            source=source,
            name=name,
            kind=kind,
            path=path,
            byte_count=0,
            line_count=0,
        )
        self._session.add(stream)
        await self._session.flush()
        return stream

    async def get(self, *, workspace_id: str, stream_id: str) -> WorkspaceLogStream | None:
        stmt = select(WorkspaceLogStream).where(
            WorkspaceLogStream.workspace_id == workspace_id,
            WorkspaceLogStream.stream_id == stream_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_workspace(self, workspace_id: str) -> list[WorkspaceLogStream]:
        stmt = (
            select(WorkspaceLogStream)
            .where(WorkspaceLogStream.workspace_id == workspace_id)
            .order_by(WorkspaceLogStream.opened_at, WorkspaceLogStream.stream_id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_validation_for_workspace(self, workspace_id: str) -> list[WorkspaceLogStream]:
        stmt = (
            select(WorkspaceLogStream)
            .where(
                WorkspaceLogStream.workspace_id == workspace_id,
                or_(
                    WorkspaceLogStream.source.in_(("validation", "setup")),
                    WorkspaceLogStream.stream_id.like("validation.%"),
                    WorkspaceLogStream.stream_id.like("setup.%"),
                ),
            )
            .order_by(WorkspaceLogStream.opened_at, WorkspaceLogStream.stream_id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def append_metadata(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        byte_delta: int,
        line_delta: int,
    ) -> WorkspaceLogStream | None:
        stream = await self.get(workspace_id=workspace_id, stream_id=stream_id)
        if stream is None:
            return None
        if byte_delta == 0 and line_delta == 0:
            return stream
        if stream.closed_at is not None:
            stream.closed_at = None
        stream.byte_count += byte_delta
        stream.line_count += line_delta

        # Fast path update without locking the Workspace ORM object
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=5)
        await self._session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .where(
                or_(
                    Workspace.last_log_at.is_(None),
                    Workspace.last_log_at < cutoff,
                )
            )
            .values(last_log_at=now, last_activity_at=now)
        )

        await self._session.flush()
        return stream

    async def close(self, *, workspace_id: str, stream_id: str) -> WorkspaceLogStream | None:
        stream = await self.get(workspace_id=workspace_id, stream_id=stream_id)
        if stream is None:
            return None
        if stream.closed_at is None:
            stream.closed_at = datetime.now(UTC)
        await self._session.flush()
        return stream
