"""API contract tests for WorkspaceResponse.claim_active."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.request_admission as request_admission
from awf.api.app import configure_database, create_app
from awf.common.config import get_settings
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.disk import DiskCheck

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")

_V2_MINIMAL_BODY = {
    "repo": {
        "url": "git@github.com:dimileeh/aira-agent.git",
        "base_branch": "development",
    },
    "task": {
        "title": "Claim active projection",
        "prompt": "Exercise claim_active on workspace reads.",
        "agent": "codex",
        "kind": "feature_branch_pr",
    },
    "workspace": {"profile_ref": "auto", "profile": None},
    "validation": {"commands": ["pytest -q"], "requested_tier": 1},
    "resources": {},
    "preflight": {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": "claim_active unit test bypasses provider auth",
    },
}
_WORKSPACE_API_TOKEN = "unit-test-workspace-api-token"
_STABLE_REQUEST_ADMISSION_CLOCK = 1000.0


def _install_stable_request_admission_limiter(state: object) -> None:
    setattr(
        state,
        request_admission._LIMITER_STATE_KEY,  # noqa: SLF001
        request_admission.RequestAdmissionLimiter(clock=lambda: _STABLE_REQUEST_ADMISSION_CLOCK),
    )


def _disk_check(*, free_bytes: int, threshold_bytes: int, ok: bool) -> DiskCheck:
    return DiskCheck(
        path="/workspace/.awf",
        checked_path="/workspace",
        total_bytes=1000,
        used_bytes=1000 - free_bytes,
        free_bytes=free_bytes,
        percent_free=free_bytes / 10,
        threshold_bytes=threshold_bytes,
        ok=ok,
        status="ok" if ok else "fail",
        reason="SUFFICIENT_DISK" if ok else "INSUFFICIENT_DISK",
        detail=None if ok else "Free disk is below AWF_MIN_FREE_DISK_BYTES.",
    )


@pytest.fixture(autouse=True)
def _provider_auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CODEX_AUTH_TOKEN", "unit-test-provider-token")
    monkeypatch.setenv("AWF_API_TOKEN", _WORKSPACE_API_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    _install_stable_request_admission_limiter(app.state)
    app.state.workspace_admission_disk_check = lambda settings: _disk_check(
        free_bytes=settings.min_free_disk_bytes + 1,
        threshold_bytes=settings.min_free_disk_bytes,
        ok=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {_WORKSPACE_API_TOKEN}"
        yield c


async def _create_workspace(client: AsyncClient) -> str:
    create = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY)
    assert create.status_code == 202
    return str(create.json()["workspace_id"])


@pytest.mark.unit
async def test_get_workspace_claim_active_true_for_live_execution_claim(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_workspace(client)
    future = datetime.now(UTC) + timedelta(minutes=10)
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.execution_claimed_by = "worker-live"
        workspace.execution_claim_expires_at = future
        await session.commit()

    response = await client.get(f"/v1/workspaces/{workspace_id}")

    assert response.status_code == 200
    assert response.json()["claim_active"] is True


@pytest.mark.unit
async def test_get_workspace_claim_active_false_without_live_claim(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_workspace(client)
    past = datetime.now(UTC) - timedelta(minutes=1)
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.execution_claimed_by = "worker-expired"
        workspace.execution_claim_expires_at = past
        await session.commit()

    response = await client.get(f"/v1/workspaces/{workspace_id}")

    assert response.status_code == 200
    assert response.json()["claim_active"] is False


@pytest.mark.unit
async def test_list_workspaces_includes_claim_active(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_workspace(client)
    future = datetime.now(UTC) + timedelta(minutes=10)
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "monitor-live"
        workspace.monitor_claim_expires_at = future
        await session.commit()

    listed = await client.get("/v1/workspaces")

    assert listed.status_code == 200
    items = listed.json()
    match = next(item for item in items if item["id"] == workspace_id)
    assert match["claim_active"] is True
