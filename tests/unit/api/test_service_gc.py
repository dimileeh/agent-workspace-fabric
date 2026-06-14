"""``POST /v1/service/gc`` route tests.

The route is the root control-plane GC trigger the thin ``awf service gc`` CLI
calls. These tests cover auth, dry-run planning, request-param mapping, and the
``partial`` envelope; the deletion mechanism itself is covered by the gc service
entrypoint unit tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")


async def _seed_completed_merged(
    engine: AsyncEngine,
    *,
    updated_at: datetime,
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title="gc candidate",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.completed.value
        workspace.updated_at = updated_at
        workspace.pr_url = "https://github.com/example/repo/pull/9"
        workspace.pr_number = 9
        workspace.pr_merge_sha = "a" * 40
        await session.commit()
        return workspace.id


class _FakeGCResult:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return self._payload


@pytest.mark.unit
async def test_service_gc_requires_api_token(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/service/gc",
        json={},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_service_gc_dry_run_returns_plan(
    client: AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    workspace_id = await _seed_completed_merged(
        engine,
        updated_at=datetime.now(UTC) - timedelta(hours=400),
    )

    response = await client.post(
        "/v1/service/gc",
        json={"min_age_hours": 24},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["status"] == "dry_run"
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["workspace_id"] == workspace_id


@pytest.mark.unit
async def test_service_gc_maps_request_params_to_entrypoint(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    captured: dict[str, object] = {}

    async def _fake_entrypoint(_session_factory: object, **kwargs: object) -> _FakeGCResult:
        captured.update(kwargs)
        return _FakeGCResult(
            {
                "dry_run": False,
                "status": "succeeded",
                "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
                "candidate_count": 1,
                "preserved_count": 0,
                "deleted_path_count": 3,
                "total_estimated_bytes": 42,
            }
        )

    with patch(
        "awf.api.routes.service.run_service_workspace_gc",
        new=AsyncMock(side_effect=_fake_entrypoint),
    ):
        response = await client.post(
            "/v1/service/gc",
            json={
                "execute": True,
                "min_age_hours": 12,
                "limit": 5,
                "statuses": ["completed"],
                "exclude_statuses": ["failed"],
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    # The request params are forwarded to the API-side reclaim entrypoint.
    assert captured["execute"] is True
    assert captured["min_age_hours"] == 12
    assert captured["limit"] == 5
    assert list(captured["include_statuses"]) == [WorkspaceStatus.completed]
    assert list(captured["exclude_statuses"]) == [WorkspaceStatus.failed]
    # GC-B wiring (#389): the route threads the host home and the (default-on)
    # base-GC flag into the entrypoint.
    assert isinstance(captured["host_home"], Path)
    assert captured["reap_claude_bases"] is True
    # #582: ``execute`` delegates the capability-gated reclaim to the worker. With
    # no fresh worker heartbeat in this test env the delegation fast-fails to a
    # structured worker-unavailable outcome (never a false ``succeeded``), while
    # the API-side reclaim (deleted_path_count 3) is still reported.
    assert payload["status"] == "partial"
    assert payload["deleted_path_count"] == 3
    assert payload["worker_reclaim"]["reason_code"] == "SERVICE_GC_WORKER_UNAVAILABLE"


@pytest.mark.unit
async def test_service_gc_passes_disabled_base_reap_flag(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    monkeypatch.setenv("AWF_CLAUDE_BASE_GC_ENABLED", "false")
    captured: dict[str, object] = {}

    async def _fake_entrypoint(_session_factory: object, **kwargs: object) -> _FakeGCResult:
        captured.update(kwargs)
        return _FakeGCResult(
            {
                "dry_run": True,
                "status": "dry_run",
                "reason_code": "CLEANUP_DRY_RUN",
                "candidate_count": 0,
                "preserved_count": 0,
                "deleted_path_count": 0,
                "total_estimated_bytes": 0,
            }
        )

    with patch(
        "awf.api.routes.service.run_service_workspace_gc",
        new=AsyncMock(side_effect=_fake_entrypoint),
    ):
        response = await client.post("/v1/service/gc", json={})

    assert response.status_code == 200, response.text
    # The operator disabled GC-B; the route forwards the flag as-is.
    assert captured["reap_claude_bases"] is False


@pytest.mark.unit
async def test_service_gc_accepts_superseded_status_filter(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``superseded`` is a terminal GC status even though it is not a
    ``WorkspaceStatus`` enum value; the request schema must accept it for both
    include and exclude filters so API clients can target/exclude that class."""
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    captured: dict[str, object] = {}

    async def _fake_entrypoint(_session_factory: object, **kwargs: object) -> _FakeGCResult:
        captured.update(kwargs)
        return _FakeGCResult(
            {
                "dry_run": True,
                "status": "dry_run",
                "reason_code": "CLEANUP_DRY_RUN",
                "candidate_count": 0,
                "preserved_count": 0,
                "deleted_path_count": 0,
                "total_estimated_bytes": 0,
            }
        )

    with patch(
        "awf.api.routes.service.run_service_workspace_gc",
        new=AsyncMock(side_effect=_fake_entrypoint),
    ):
        response = await client.post(
            "/v1/service/gc",
            json={
                "statuses": ["superseded"],
                "exclude_statuses": ["superseded"],
            },
        )

    assert response.status_code == 200, response.text
    assert list(captured["include_statuses"]) == ["superseded"]
    assert list(captured["exclude_statuses"]) == ["superseded"]


@pytest.mark.unit
async def test_service_gc_returns_partial_envelope(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))

    fake = _FakeGCResult(
        {
            "dry_run": False,
            "status": "partial",
            "reason_code": "CLEANUP_EXECUTION_PARTIAL",
            "candidate_count": 1,
            "preserved_count": 0,
            "deleted_path_count": 0,
            "total_estimated_bytes": 0,
            "delete_errors": [{"reason_code": "PATH_DELETE_PERMISSION_DENIED", "kind": "auth"}],
        }
    )

    with patch(
        "awf.api.routes.service.run_service_workspace_gc",
        new=AsyncMock(return_value=fake),
    ):
        response = await client.post("/v1/service/gc", json={"execute": True})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "partial"
    # The API-side per-path delete errors still pass through unchanged.
    assert payload["delete_errors"][0]["reason_code"] == "PATH_DELETE_PERMISSION_DENIED"
    # #582: with no fresh worker heartbeat the execute delegation downgrades the
    # headline reason to the worker-unavailable code (the more actionable signal),
    # while the per-path errors remain inspectable in ``delete_errors``.
    assert payload["reason_code"] == "SERVICE_GC_WORKER_UNAVAILABLE"
