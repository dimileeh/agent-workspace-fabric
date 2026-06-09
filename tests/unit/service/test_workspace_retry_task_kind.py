"""Task-kind persistence tests for workspace retry."""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker

from awf.service.workspaces import WorkspaceService
from tests.postgres import create_postgres_test_engine

pytestmark = pytest.mark.unit


def _request(
    *,
    task_kind: str = "feature_branch_pr",
    provider_readiness_override: bool = True,
):
    from awf.api.schemas import WorkspaceCreateRequest

    payload: dict[str, object] = {
        "repo": {"url": "git@github.com:example/retryable.git", "base_branch": "development"},
        "task": {
            "title": "Retry flaky validation",
            "prompt": "Fix the intermittent validation failure.",
            "agent": "codex",
            "kind": task_kind,
            "external_id": "TICKET-RETRY",
            "task_class": "test_task",
            "owned_paths": ["src/awf/retry/**"],
            "auto_merge": False,
            "initial_review_grace_period_seconds": 30,
        },
        "workspace": {"profile_ref": "python", "profile": None},
        "validation": {"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        "resources": {},
    }
    if provider_readiness_override:
        payload["preflight"] = {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "retry service test fixture",
        }
    return WorkspaceCreateRequest.model_validate(payload)


async def _mark_failed(
    factory, workspace_id, *, branch_name="codex/old-attempt", release_runtime=True
):
    from awf.db.enums import WorkspaceStatus
    from awf.db.repositories import WorkspaceRepository

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = "validation_failure"
        workspace.failure_message = "pytest failed"
        workspace.branch_name = branch_name
        workspace.compose_project_name = "awf_old_attempt"
        assert workspace.resolved_profile is not None
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        if release_runtime:
            from awf.db.repositories.base import (
                TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            )

            await repo.add_event(
                workspace,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                payload={},
            )
        await session.commit()


async def _retry_with_preflight_override(service, workspace_id):
    return await service.retry_workspace(
        workspace_id,
        provider_readiness_override=True,
        provider_readiness_override_reason="retry task-kind test",
    )


@pytest.mark.unit
async def test_retry_persists_task_kind_without_post_insert_update() -> None:
    engine = await create_postgres_test_engine()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = WorkspaceService(factory)
    first = await service.create(_request(task_kind="sync_release_pr"))
    await _mark_failed(factory, first.id)

    statements: list[str] = []

    def record_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        await _retry_with_preflight_override(service, first.id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
        await engine.dispose()

    task_kind_updates = [
        statement
        for statement in statements
        if statement.startswith("update workspaces") and "task_kind" in statement
    ]
    assert task_kind_updates == []
