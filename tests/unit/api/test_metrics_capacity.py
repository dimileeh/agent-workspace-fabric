"""Resource saturation metrics API tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.disk import DiskCheck


async def _workspace(
    engine: AsyncEngine,
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
) -> str:
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
        await session.commit()
        return workspace.id


async def _workspace_with_reservation(
    engine: AsyncEngine,
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    steady_cpu: float,
    steady_memory_gb: float,
    peak_cpu: float,
    peak_memory_gb: float,
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/metrics-api.git",
            branch_base="main",
            task_title=f"{status.value} reserved workspace",
            task_prompt="Collect workspace reliability metrics.",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=None,
            idempotency_key=f"metrics-api-reservation:{workspace.id}",
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        await ResourceReservationRepository(session).create(
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            node_id="local",
            steady_cpu=steady_cpu,
            steady_memory_gb=steady_memory_gb,
            peak_cpu=peak_cpu,
            peak_memory_gb=peak_memory_gb,
            disk_mb=None,
            phase="workspace_lifecycle",
        )
        await session.commit()
        return workspace.id


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
async def test_resource_saturation_endpoint_uses_persisted_active_reservations(
    metrics_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
        workspace_steady_cpu=3.0,
        workspace_steady_memory_gb=10.0,
        workspace_peak_cpu=6.0,
        workspace_peak_memory_gb=16.0,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
    )
    now = datetime.now(UTC)
    await _workspace_with_reservation(
        engine,
        status=WorkspaceStatus.running,
        updated_at=now - timedelta(minutes=5),
        steady_cpu=4.0,
        steady_memory_gb=12.0,
        peak_cpu=8.0,
        peak_memory_gb=24.0,
    )
    await _workspace(
        engine,
        status=WorkspaceStatus.ready,
        updated_at=now - timedelta(minutes=4),
    )

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_counts"]["active_total"] == 2
    assert body["reserved_resources"] == {
        "active_workspace_count": 2,
        "steady_cpu": 7.0,
        "steady_memory_gb": 22.0,
        "peak_cpu": 14.0,
        "peak_memory_gb": 40.0,
    }


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
