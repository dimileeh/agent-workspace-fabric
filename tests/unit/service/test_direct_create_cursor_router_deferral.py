"""Direct-create deferral of Cursor Router preflight for unresolved auto profiles."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings
from awf.db.session import make_session_factory
from awf.service import workspaces_create
from awf.service.disk import DiskCheck
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _disk_check() -> DiskCheck:
    return DiskCheck(
        path="/tmp/awf-work",
        checked_path="/tmp",
        total_bytes=20 * 1024 * 1024 * 1024,
        used_bytes=8 * 1024 * 1024 * 1024,
        free_bytes=12 * 1024 * 1024 * 1024,
        percent_free=60.0,
        threshold_bytes=1024,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
        detail=None,
    )


def _cursor_auto_create_request(
    *,
    profile_ref: str = "auto",
    profile: dict[str, object] | None = None,
    provider_readiness_override: bool = False,
) -> WorkspaceCreateRequest:
    workspace: dict[str, object] = {"profile_ref": profile_ref, "profile": profile}
    preflight: dict[str, object] = {}
    if provider_readiness_override:
        preflight = {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "test override",
        }
    return WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/cursor-auto.git", "base_branch": "main"},
        task={
            "title": "Cursor Auto create",
            "prompt": "Exercise Router preflight deferral.",
            "agent": "cursor",
            "kind": "feature_branch_pr",
            "cursor_auto_mode": "intelligence",
        },
        workspace=workspace,
        validation={"commands": ["pytest -q"], "requested_tier": 1},
        preflight=preflight,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_direct_create_defers_router_preflight_for_unresolved_auto_profile(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """profile_ref=auto cannot see repo-local CURSOR_API_KEY until checkout.

    Create must not probe Router with the worker key or record a preflight that
    would skip the provision-time recheck with resolved profile credentials.
    """
    called = False

    async def _must_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {
            "blocks_launch": False,
            "reason_code": "CURSOR_ROUTER_AVAILABLE",
            "message": "should not run at create for unresolved auto",
        }

    monkeypatch.setattr(
        workspaces_create,
        "_selected_provider_preflight_for_task_async",
        _must_not_run,
    )
    monkeypatch.setenv("CURSOR_API_KEY", "worker-cursor-key")

    async with factory() as session:
        created = await workspaces_create.create_workspace_row(
            session,
            _cursor_auto_create_request(),
            settings=Settings(_env_file=None),
            disk_check=_disk_check(),
        )
        await session.commit()

    assert called is False
    assert "provider_readiness_preflight" not in (created.task_policy or {})
    assert (created.task_policy or {}).get("cursor_auto_mode") == "intelligence"
    assert created.resolved_profile is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_direct_create_runs_router_preflight_when_inline_profile_resolves(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inline profile is resolvable at create; Router preflight must still run."""
    called = False

    async def _ready(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {
            "blocks_launch": False,
            "reason_code": "CURSOR_ROUTER_AVAILABLE",
            "message": "Router available for inline profile key.",
        }

    monkeypatch.setattr(
        workspaces_create,
        "_selected_provider_preflight_for_task_async",
        _ready,
    )
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    async with factory() as session:
        created = await workspaces_create.create_workspace_row(
            session,
            _cursor_auto_create_request(
                profile={
                    "name": "inline-cursor",
                    "runtime": {"environment": {"CURSOR_API_KEY": "profile-cursor-key"}},
                },
            ),
            settings=Settings(_env_file=None),
            disk_check=_disk_check(),
        )
        await session.commit()

    assert called is True
    assert (created.task_policy or {})["provider_readiness_preflight"]["reason_code"] == (
        "CURSOR_ROUTER_AVAILABLE"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_direct_create_runs_router_preflight_when_override_set(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator override keeps create-time preflight even for unresolved auto."""
    called = False

    async def _ready(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {
            "blocks_launch": False,
            "reason_code": "PROVIDER_READINESS_OVERRIDE",
            "message": "override",
            "override": True,
        }

    monkeypatch.setattr(
        workspaces_create,
        "_selected_provider_preflight_for_task_async",
        _ready,
    )

    async with factory() as session:
        created = await workspaces_create.create_workspace_row(
            session,
            _cursor_auto_create_request(provider_readiness_override=True),
            settings=Settings(_env_file=None),
            disk_check=_disk_check(),
        )
        await session.commit()

    assert called is True
    assert "provider_readiness_preflight" in (created.task_policy or {})
