"""Workspace, WorkspaceEvent, Operation, LogStream, and SecretLease database repositories."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import (
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.ids import (
    new_secret_lease_id,
)
from awf.db.models import (
    Workspace,
    WorkspaceSecretLease,
)
from awf.db.repositories.base import (
    _SECRET_LEASE_ACTIVE_STATUSES,
    _SECRET_LEASE_REVOCABLE_STATUSES,
    SECRET_LEASE_AUDIT_EVENT_TYPE,
    SECRET_LEASE_STATUS_EXPIRED,
    SECRET_LEASE_STATUS_ISSUED,
    SECRET_LEASE_STATUS_MOUNTED,
    SECRET_LEASE_STATUS_REVOKED,
    SecretLeaseIssue,
    WorkspaceEventCreate,
    _declared_lease_requires_reissue,
    _group_leases_by_workspace,
    _IssuedSecretLease,
    _lease_audit_payload,
    _reissue_declared_lease,
    _sanitize_metadata,
    _secret_lease_declaration_key,
    _secret_lease_insert_if_absent_stmt,
    resolve_session_dialect_name,
)


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
            from awf.db.repositories.workspace_repo import set_committed_value

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
        from awf.db.repositories.workspace_repo import WorkspaceRepository

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
