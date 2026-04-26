"""Workspace reliability metrics API tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.disk import DiskCheck


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


def _disk_check(
    settings: Settings,
    *,
    ok: bool = True,
    free_bytes: int | None = None,
    reason: str = "SUFFICIENT_DISK",
) -> DiskCheck:
    threshold = settings.min_free_disk_bytes
    free = threshold + 1 if free_bytes is None else free_bytes
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=max(free + 1, threshold + 1),
        used_bytes=1,
        free_bytes=free,
        percent_free=99.0,
        threshold_bytes=threshold,
        ok=ok,
        status="ok" if ok else "fail",
        reason=reason,
        detail=None if ok else "Free disk is below the configured admission threshold.",
    )


@pytest.fixture
async def metrics_app_and_client(
    engine: AsyncEngine,
) -> AsyncIterator[tuple[Any, AsyncClient]]:
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield app, c


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


@pytest.mark.unit
async def test_resource_saturation_endpoint_reports_local_capacity_inputs(
    metrics_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
        worker_max_concurrent_provisions=5,
        worker_max_concurrent_executions=2,
        workspace_steady_cpu=2.0,
        workspace_steady_memory_gb=7.5,
        workspace_peak_cpu=4.0,
        workspace_peak_memory_gb=12.0,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
    )
    now = datetime.now(UTC)
    for status in (
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.completed,
    ):
        await _workspace(engine, status=status, updated_at=now - timedelta(minutes=5))

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_counts"]["active_total"] == 3
    assert body["workspace_counts"]["ready"] == 1
    assert body["workspace_counts"]["running"] == 1
    assert body["workspace_counts"]["validating"] == 1
    assert body["workspace_counts"]["by_status"][WorkspaceStatus.completed.value] == 1
    assert body["worker"] == {
        "max_concurrent_provisions": 5,
        "max_concurrent_executions": 2,
    }
    assert body["resource_defaults"] == {
        "steady_cpu": 2.0,
        "steady_memory_gb": 7.5,
        "peak_cpu": 4.0,
        "peak_memory_gb": 12.0,
    }
    assert body["reserved_resources"] == {
        "active_workspace_count": 3,
        "steady_cpu": 6.0,
        "steady_memory_gb": 22.5,
        "peak_cpu": 12.0,
        "peak_memory_gb": 36.0,
    }
    assert body["concurrency"]["execution"] == {
        "limit": 2,
        "in_use": 2,
        "queued": 1,
        "available": 0,
    }
    assert body["disk"]["reason"] == "SUFFICIENT_DISK"
    assert body["admission"]["ok"] is True
    assert body["admission"]["status"] == "saturated"
    assert body["admission"]["reason"] == "WORKER_EXECUTION_CONCURRENCY_SATURATED"


@pytest.mark.unit
async def test_resource_saturation_endpoint_explains_disk_admission_denial(
    metrics_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=False,
        free_bytes=100,
        reason="INSUFFICIENT_DISK",
    )

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    body = response.json()
    assert body["disk"]["ok"] is False
    assert body["disk"]["reason"] == "INSUFFICIENT_DISK"
    assert body["admission"]["ok"] is False
    assert body["admission"]["status"] == "blocked"
    assert body["admission"]["reason"] == "INSUFFICIENT_DISK"
    assert "free disk" in body["admission"]["detail"].lower()
