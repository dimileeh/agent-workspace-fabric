"""Workspace transition race regressions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


@pytest.mark.unit
async def test_transition_keeps_retrying_repeated_finished_teardown_races(
    session: AsyncSession,
) -> None:
    repo = WorkspaceRepository(session)
    workspace = await repo.create(
        repo_url="git@github.com:example/a.git",
        branch_base="development",
        task_title="t",
        task_prompt="p",
        agent=AgentRuntime.codex.value,
        test_commands=[],
    )
    await session.commit()

    inserted_operation_ids: list[str] = []
    completed_operation_ids: list[str] = []
    bind = session.get_bind()

    def finish_two_stops_before_diagnostics(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del cursor, parameters, context, executemany
        normalized = " ".join(statement.lower().split())
        if len(inserted_operation_ids) < 2 and normalized.startswith("update workspaces set "):
            operation_id = f"op_transition_teardown_toc_tou_{len(inserted_operation_ids)}"
            inserted_operation_ids.append(operation_id)
            conn.execute(
                text(
                    """
                    INSERT INTO operations (
                        id, workspace_id, type, status, created_at, started_at
                    )
                    VALUES (
                        :operation_id,
                        :workspace_id,
                        :operation_type,
                        :operation_status,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "operation_id": operation_id,
                    "workspace_id": workspace.id,
                    "operation_type": OperationType.stop.value,
                    "operation_status": OperationStatus.running.value,
                },
            )
            return
        if (
            len(completed_operation_ids) < len(inserted_operation_ids)
            and normalized.startswith("select operations.")
            and " from operations " in normalized
        ):
            operation_id = inserted_operation_ids[len(completed_operation_ids)]
            completed_operation_ids.append(operation_id)
            conn.execute(
                text(
                    """
                    UPDATE operations
                    SET status = :operation_status
                    WHERE id = :operation_id
                    """
                ),
                {
                    "operation_id": operation_id,
                    "operation_status": OperationStatus.succeeded.value,
                },
            )

    event.listen(bind, "before_cursor_execute", finish_two_stops_before_diagnostics)
    try:
        await repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="WORKER_CLAIMED",
        )
    finally:
        event.remove(
            bind,
            "before_cursor_execute",
            finish_two_stops_before_diagnostics,
        )

    assert inserted_operation_ids == [
        "op_transition_teardown_toc_tou_0",
        "op_transition_teardown_toc_tou_1",
    ]
    assert completed_operation_ids == inserted_operation_ids
    assert workspace.status == WorkspaceStatus.provisioning.value
    assert workspace.version == 2


@pytest.mark.unit
async def test_transition_caps_repeated_finished_teardown_races(
    session: AsyncSession,
) -> None:
    repo = WorkspaceRepository(session)
    workspace = await repo.create(
        repo_url="git@github.com:example/a.git",
        branch_base="development",
        task_title="t",
        task_prompt="p",
        agent=AgentRuntime.codex.value,
        test_commands=[],
    )
    await session.commit()

    inserted_operation_ids: list[str] = []
    bind = session.get_bind()

    def finish_every_stop_before_diagnostics(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del cursor, parameters, context, executemany
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("update workspaces set "):
            operation_id = f"op_toc_tou_cap_{len(inserted_operation_ids)}"
            inserted_operation_ids.append(operation_id)
            conn.execute(
                text(
                    """
                    INSERT INTO operations (
                        id, workspace_id, type, status, created_at, started_at
                    )
                    VALUES (
                        :operation_id,
                        :workspace_id,
                        :operation_type,
                        :operation_status,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "operation_id": operation_id,
                    "workspace_id": workspace.id,
                    "operation_type": OperationType.stop.value,
                    "operation_status": OperationStatus.running.value,
                },
            )
            return
        if normalized.startswith("select operations.") and " from operations " in normalized:
            conn.execute(
                text(
                    """
                    UPDATE operations
                    SET status = :operation_status
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {
                    "workspace_id": workspace.id,
                    "operation_status": OperationStatus.succeeded.value,
                },
            )

    event.listen(bind, "before_cursor_execute", finish_every_stop_before_diagnostics)
    try:
        with pytest.raises(RuntimeError, match="teardown contention retry limit"):
            await asyncio.wait_for(
                repo.transition(
                    workspace,
                    to=WorkspaceStatus.provisioning,
                    reason_code="WORKER_CLAIMED",
                ),
                timeout=2.0,
            )
    finally:
        event.remove(
            bind,
            "before_cursor_execute",
            finish_every_stop_before_diagnostics,
        )

    assert inserted_operation_ids
    assert workspace.status == WorkspaceStatus.requested.value
    assert workspace.version == 1


@pytest.mark.unit
async def test_transition_locks_workspace_before_teardown_guarded_update(
    session: AsyncSession,
) -> None:
    repo = WorkspaceRepository(session)
    workspace = await repo.create(
        repo_url="git@github.com:example/a.git",
        branch_base="development",
        task_title="t",
        task_prompt="p",
        agent=AgentRuntime.codex.value,
        test_commands=[],
    )
    await session.commit()

    statements: list[str] = []
    bind = session.get_bind()

    def capture_statement(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(" ".join(statement.lower().split()))

    event.listen(bind, "before_cursor_execute", capture_statement)
    try:
        await repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="WORKER_CLAIMED",
        )
    finally:
        event.remove(bind, "before_cursor_execute", capture_statement)

    lock_index = next(
        i
        for i, statement in enumerate(statements)
        if " from workspaces " in statement and " for update" in statement
    )
    update_index = next(
        i
        for i, statement in enumerate(statements)
        if statement.startswith("update workspaces set ")
    )
    assert lock_index < update_index


@pytest.mark.unit
async def test_teardown_operation_creation_locks_workspace_before_insert(
    session: AsyncSession,
) -> None:
    workspace = await WorkspaceRepository(session).create(
        repo_url="git@github.com:example/a.git",
        branch_base="development",
        task_title="t",
        task_prompt="p",
        agent=AgentRuntime.codex.value,
        test_commands=[],
    )
    await session.commit()

    statements: list[str] = []
    bind = session.get_bind()

    def capture_statement(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(" ".join(statement.lower().split()))

    event.listen(bind, "before_cursor_execute", capture_statement)
    try:
        await OperationRepository(session).create(
            workspace_id=workspace.id,
            operation_type=OperationType.stop,
            status=OperationStatus.running,
        )
    finally:
        event.remove(bind, "before_cursor_execute", capture_statement)

    lock_index = next(
        i
        for i, statement in enumerate(statements)
        if " from workspaces " in statement and " for update" in statement
    )
    insert_index = next(
        i
        for i, statement in enumerate(statements)
        if statement.startswith("insert into operations ")
    )
    assert lock_index < insert_index
