"""Data access layer for control-plane entities.

Repositories encapsulate SQL queries so route handlers and workers don't sprinkle
SQLAlchemy calls everywhere. Rules:

- Repositories do NOT commit; callers manage transactions.
- Repositories route every ``status`` mutation through ``WorkspaceStateMachine``.
- Repositories are the only code that writes to ``workspace_events``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.ids import new_event_id, new_workspace_id
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent


class WorkspaceRepository:
    """CRUD + state transitions for workspaces.

    Holds a reference to an ``AsyncSession`` for the life of one logical unit
    of work. Do not reuse across request boundaries.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
        env_profile: str | None = None,
        profile_ref: str | None = None,
        requested_profile: dict[str, Any] | None = None,
        resolved_profile: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Workspace:
        """Create a new workspace in ``requested`` status and emit a creation event.

        Does not commit — the caller owns the transaction boundary.
        """
        workspace = Workspace(
            id=new_workspace_id(),
            status=WorkspaceStatus.requested.value,
            version=1,
            repo_url=repo_url,
            branch_base=branch_base,
            task_title=task_title,
            task_prompt=task_prompt,
            task_external_id=task_external_id,
            agent=agent,
            env_profile=env_profile,
            profile_ref=profile_ref,
            requested_profile=requested_profile,
            resolved_profile=resolved_profile,
            test_commands=test_commands,
            requires_database=requires_database,
            idempotency_key=idempotency_key,
        )
        # Append to the relationship so both the session AND the in-memory
        # ``workspace.events`` collection are populated. A bare ``session.add(event)``
        # would only add the row; callers reading ``workspace.events`` would then
        # trigger a lazy load, which fails in async contexts.
        workspace.events.append(
            WorkspaceEvent(
                id=new_event_id(),
                event_type="workspace.created",
                old_state=None,
                new_state=WorkspaceStatus.requested.value,
                reason_code="CREATED",
            )
        )
        self._session.add(workspace)
        await self._session.flush()
        return workspace

    async def get(self, workspace_id: str) -> Workspace | None:
        return await self._session.get(Workspace, workspace_id)

    async def get_by_idempotency_key(self, key: str) -> Workspace | None:
        stmt = select(Workspace).where(Workspace.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_events(
        self,
        *,
        workspace_id: str | None = None,
        limit: int = 50,
    ) -> list[WorkspaceEvent]:
        """Return events newest-first, optionally filtered by ``workspace_id``.

        ``occurred_at`` is the canonical ordering key; ``id`` is the tiebreaker
        so events recorded in the same transaction (identical server-side
        default timestamp on SQLite) have a stable, deterministic order.
        """
        stmt = select(WorkspaceEvent)
        if workspace_id is not None:
            stmt = stmt.where(WorkspaceEvent.workspace_id == workspace_id)
        stmt = stmt.order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc()).limit(
            limit
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list(self, *, limit: int = 50) -> list[Workspace]:
        stmt = select(Workspace).order_by(Workspace.created_at.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def transition(
        self,
        workspace: Workspace,
        *,
        to: WorkspaceStatus,
        reason_code: str,
    ) -> Workspace:
        """Move a workspace to the given status, recording an event.

        Validates the transition through ``WorkspaceStateMachine``. Bumps
        ``version`` for optimistic concurrency on downstream updates.
        """
        current = WorkspaceStatus(workspace.status)
        WorkspaceStateMachine.assert_transition(current, to)

        old_state = workspace.status
        workspace.status = to.value
        workspace.version += 1

        workspace.events.append(
            WorkspaceEvent(
                id=new_event_id(),
                event_type="workspace.state_changed",
                old_state=old_state,
                new_state=to.value,
                reason_code=reason_code,
            )
        )
        return workspace
