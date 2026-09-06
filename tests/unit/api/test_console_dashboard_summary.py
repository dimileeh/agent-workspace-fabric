"""Console dashboard-summary API tests (schema_version=1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.unit.helpers import create_workspace


@pytest.mark.unit
async def test_dashboard_summary_requires_auth(client: AsyncClient) -> None:
    response = await client.get(
        "/v1/console/dashboard-summary",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_dashboard_summary_independent_of_capacity(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summary must not call Docker/capacity scanners."""

    now = datetime.now(UTC)
    for status in (
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.pushing,
        WorkspaceStatus.blocked,
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.requested,
        WorkspaceStatus.requested,
    ):
        await create_workspace(engine, status=status, updated_at=now)

    flagged = await create_workspace(engine, status=WorkspaceStatus.monitoring_pr, updated_at=now)
    factory = make_session_factory(engine)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            flagged, reason="blocking review requires a human", now=now
        )
        await session.commit()

    await create_workspace(
        engine,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=1),
    )
    await create_workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=2),
    )
    await create_workspace(
        engine,
        status=WorkspaceStatus.cancelled,
        updated_at=now - timedelta(hours=3),
    )

    docker_probe = AsyncMock(side_effect=AssertionError("Docker must not be probed"))
    monkeypatch.setattr(
        "awf.service.orphan_resources.scan_docker_resources",
        docker_probe,
    )
    monkeypatch.setattr(
        "awf.service.local_capacity.detect_local_capacity",
        docker_probe,
    )

    response = await client.get("/v1/console/dashboard-summary")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["scope"] == "local"
    assert body["coverage"]["status"] == "complete"
    counts = body["counts"]
    # running+validating+pushing = 3 executing; blocked+2 monitoring + 2 requested + 3 exec = 8 active
    assert counts["executing"] == 3
    assert counts["awaiting_operator"] == 1
    assert counts["awaiting_human"] == 1
    assert counts["monitoring_pr"] == 2
    assert counts["queued"] == 2
    assert counts["retrying"] == 0
    assert counts["active"] == 8
    assert counts["completed_last_window"] == 1
    assert counts["failed_last_window"] == 1
    assert counts["cancelled_last_window"] == 1
    assert body["overlap"]["awaiting_human_subset_of_monitoring_pr"] is True
    assert body["overlap"]["awaiting_operator_in_active_not_executing"] is True
    assert body["window"]["anchor"] == "generated_at"
    assert body["window"]["since_hours"] == 24
    docker_probe.assert_not_called()


@pytest.mark.unit
async def test_dashboard_summary_null_not_zero_on_partial(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service.console_dashboard_summary import (
        ConsoleDashboardCounts,
        ConsoleDashboardCoverage,
        ConsoleDashboardOverlap,
        ConsoleDashboardSummary,
        ConsoleDashboardWindow,
    )

    generated = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)

    async def _partial_summary(*_args, **_kwargs):
        return ConsoleDashboardSummary(
            schema_version=1,
            scope="local",
            generated_at=generated,
            as_of=generated,
            last_success_at=generated,
            window=ConsoleDashboardWindow(
                anchor="generated_at",
                since_hours=24,
                start=generated - timedelta(hours=24),
            ),
            coverage=ConsoleDashboardCoverage(
                status="partial",
                notes=("queued_count_unavailable",),
            ),
            counts=ConsoleDashboardCounts(
                active=3,
                executing=2,
                monitoring_pr=1,
                awaiting_operator=0,
                awaiting_human=0,
                retrying=0,
                queued=None,
                completed_last_window=1,
                cancelled_last_window=None,
                failed_last_window=0,
            ),
            overlap=ConsoleDashboardOverlap(
                awaiting_human_subset_of_monitoring_pr=True,
                awaiting_operator_in_active_not_executing=True,
                retrying_in_active_not_executing=True,
            ),
        )

    monkeypatch.setattr(
        "awf.api.routes.console.summarize_console_dashboard_for_session",
        _partial_summary,
    )
    response = await client.get("/v1/console/dashboard-summary")
    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["status"] == "partial"
    assert body["counts"]["queued"] is None
    assert body["counts"]["cancelled_last_window"] is None
    assert "queued" in body["counts"]
    assert body["counts"]["failed_last_window"] == 0
