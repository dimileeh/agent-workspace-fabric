"""Workspace reliability metrics API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.common.config import get_settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory


async def _workspace(
    engine: AsyncEngine,
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    failure_reason: FailureReason | None = None,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/metrics-api.git",
            branch_base="main",
            task_title=f"{status.value} workspace",
            task_prompt="Collect workspace reliability metrics.",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        workspace.failure_reason = failure_reason.value if failure_reason is not None else None
        await session.commit()


def _zero_status_counts() -> dict[str, int]:
    return {status.value: 0 for status in WorkspaceStatus}


@pytest.mark.unit
async def test_workspace_summary_returns_zero_counts_for_empty_db(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/metrics/workspaces/summary")

    assert response.status_code == 200
    body = response.json()
    generated_at = datetime.fromisoformat(body["generated_at"])
    window_start = datetime.fromisoformat(body["window_start"])
    assert window_start == generated_at - timedelta(hours=24)
    assert body["since_hours"] == 24
    assert body["status_counts"] == _zero_status_counts()
    assert body["failure_reason_counts"] == {}
    assert body["active_count"] == 0
    assert body["destroying_count"] == 0
    assert body["completed_count"] == 0
    assert body["failed_count"] == 0
    assert body["cancelled_count"] == 0
    assert body["destroyed_count"] == 0
    assert body["cleanup_failure_count"] == 0


@pytest.mark.unit
async def test_workspace_summary_rolls_up_mixed_statuses_and_failure_reasons(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    for status in (
        WorkspaceStatus.requested,
        WorkspaceStatus.running,
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.destroying,
        WorkspaceStatus.completed,
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
    ):
        await _workspace(engine, status=status, updated_at=now - timedelta(minutes=5))
    await _workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=5),
        failure_reason=FailureReason.agent_failure,
    )
    await _workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=5),
        failure_reason=FailureReason.cleanup_failure,
    )

    response = await client.get("/v1/metrics/workspaces/summary")

    assert response.status_code == 200
    body = response.json()
    expected_status_counts = _zero_status_counts()
    expected_status_counts.update(
        {
            WorkspaceStatus.requested.value: 1,
            WorkspaceStatus.running.value: 1,
            WorkspaceStatus.monitoring_pr.value: 1,
            WorkspaceStatus.destroying.value: 1,
            WorkspaceStatus.completed.value: 1,
            WorkspaceStatus.failed.value: 2,
            WorkspaceStatus.cancelled.value: 1,
            WorkspaceStatus.destroyed.value: 1,
        }
    )
    assert body["status_counts"] == expected_status_counts
    assert body["failure_reason_counts"] == {
        FailureReason.agent_failure.value: 1,
        FailureReason.cleanup_failure.value: 1,
    }
    assert body["active_count"] == 4
    assert body["destroying_count"] == 1
    assert body["completed_count"] == 1
    assert body["failed_count"] == 2
    assert body["cancelled_count"] == 1
    assert body["destroyed_count"] == 1
    assert body["cleanup_failure_count"] == 1


@pytest.mark.unit
async def test_workspace_summary_filters_by_since_hours(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    await _workspace(
        engine,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=3),
    )
    await _workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=30),
        failure_reason=FailureReason.validation_failure,
    )

    response = await client.get("/v1/metrics/workspaces/summary", params={"since_hours": 1})

    assert response.status_code == 200
    body = response.json()
    expected_status_counts = _zero_status_counts()
    expected_status_counts[WorkspaceStatus.failed.value] = 1
    assert body["since_hours"] == 1
    assert body["status_counts"] == expected_status_counts
    assert body["failure_reason_counts"] == {FailureReason.validation_failure.value: 1}
    assert body["completed_count"] == 0
    assert body["failed_count"] == 1
    assert body["destroying_count"] == 0


@pytest.mark.unit
@pytest.mark.parametrize("since_hours", ["0", "169"])
async def test_workspace_summary_validates_since_hours_bounds(
    client: AsyncClient,
    since_hours: str,
) -> None:
    response = await client.get(
        "/v1/metrics/workspaces/summary",
        params={"since_hours": since_hours},
    )

    assert response.status_code == 422


@pytest.mark.unit
async def test_workspace_summary_is_token_free_when_api_token_is_configured(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    get_settings.cache_clear()

    try:
        response = await client.get("/v1/metrics/workspaces/summary")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
