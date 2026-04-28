"""Workspace reliability metrics API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

from awf.common.config import get_settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.session import make_session_factory
from tests.unit.helpers import create_workspace, zero_status_counts


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
    assert body["status_counts"] == zero_status_counts()
    assert body["failure_reason_counts"] == {}
    assert body["active_count"] == 0
    assert body["destroying_count"] == 0
    assert body["completed_count"] == 0
    assert body["failed_count"] == 0
    assert body["cancelled_count"] == 0
    assert body["destroyed_count"] == 0
    assert body["cleanup_failure_count"] == 0
    assert body["stuck_count"] == 0
    assert body["actionable_reason_count"] == 0
    assert body["unactionable_reason_count"] == 0


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
        await create_workspace(engine, status=status, updated_at=now - timedelta(minutes=5))
    await create_workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=5),
        failure_reason=FailureReason.agent_failure,
    )
    await create_workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=5),
        failure_reason=FailureReason.cleanup_failure,
    )

    response = await client.get("/v1/metrics/workspaces/summary")

    assert response.status_code == 200
    body = response.json()
    expected_status_counts = zero_status_counts()
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
    assert body["stuck_count"] == 0
    assert body["actionable_reason_count"] == 2
    assert body["unactionable_reason_count"] == 2


@pytest.mark.unit
async def test_workspace_summary_filters_by_since_hours(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    await create_workspace(
        engine,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=3),
    )
    await create_workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=30),
        failure_reason=FailureReason.validation_failure,
    )

    response = await client.get("/v1/metrics/workspaces/summary", params={"since_hours": 1})

    assert response.status_code == 200
    body = response.json()
    expected_status_counts = zero_status_counts()
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
async def test_metrics_route_functions_return_response_models_directly(
    engine: AsyncEngine,
    tmp_path,
) -> None:
    from awf.api.routes.metrics import (
        get_failure_analysis_summary,
        get_resource_saturation_summary,
        get_slo_metrics_summary,
        get_workspace_reliability_summary,
    )
    from awf.common.config import Settings

    factory = make_session_factory(engine)
    settings = Settings(
        _env_file=None,
        work_dir=str(tmp_path),
        min_free_disk_bytes=0,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/metrics/resources/saturation",
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
    )

    async with factory() as session:
        failure = await get_failure_analysis_summary(session=session)
        reliability = await get_workspace_reliability_summary(
            settings=settings,
            session=session,
        )
        saturation = await get_resource_saturation_summary(
            request=request,
            settings=settings,
            session=session,
        )
        slo = await get_slo_metrics_summary(
            settings=settings,
            session=session,
        )

    assert failure.total_failed_workspaces == 0
    assert reliability.active_count == 0
    assert saturation.disk.reason == "SUFFICIENT_DISK"
    assert slo.creation_total == 0


@pytest.mark.unit
async def test_slo_endpoint_returns_zero_counts_for_empty_db(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/metrics/slo")

    assert response.status_code == 200
    body = response.json()
    assert body["since_hours"] == 24
    assert body["creation_total"] == 0
    assert body["creation_succeeded"] == 0
    assert body["creation_failed"] == 0
    assert body["creation_cancelled"] == 0
    assert body["cleanup_total"] == 0
    assert body["cleanup_succeeded"] == 0
    assert body["cleanup_failure_count"] == 0
    assert body["stuck_running_count"] == 0
    assert body["stuck_with_reason_count"] == 0
    assert body["recovery_total"] == 0
    assert body["recovery_succeeded"] == 0
    assert body["recovery_failed_count"] == 0
    assert body["monitor_completed_total"] == 0
    assert body["completed_after_monitor_count"] == 0
    assert body["monitor_stuck_count"] == 0
    assert body["actionable_failure_count"] == 0
    assert body["unactionable_failure_count"] == 0


@pytest.mark.unit
async def test_slo_endpoint_respects_since_hours_param(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    await create_workspace(
        engine,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(minutes=10),
    )

    response = await client.get("/v1/metrics/slo", params={"since_hours": 168})

    assert response.status_code == 200
    body = response.json()
    assert body["since_hours"] == 168
    assert body["creation_total"] == 1


@pytest.mark.unit
async def test_slo_endpoint_rejects_invalid_since_hours(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/metrics/slo", params={"since_hours": 0})

    assert response.status_code == 422


@pytest.mark.unit
async def test_slo_endpoint_returns_expected_fields_after_seeding(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    await create_workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=10),
        failure_reason=FailureReason.agent_failure,
    )

    response = await client.get("/v1/metrics/slo")

    assert response.status_code == 200
    body = response.json()
    assert body["creation_total"] >= 1
    assert body["creation_failed"] >= 1
    assert body["actionable_failure_count"] >= 1


@pytest.mark.unit
async def test_slo_endpoint_backward_compatible(
    client: AsyncClient,
) -> None:
    response_ws = await client.get("/v1/metrics/workspaces/summary")
    response_fail = await client.get("/v1/metrics/failures/summary")
    response_sat = await client.get("/v1/metrics/resources/saturation")

    assert response_ws.status_code == 200
    assert response_fail.status_code == 200
    assert response_sat.status_code == 200
