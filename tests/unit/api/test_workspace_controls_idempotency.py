"""Strict idempotency and version checks for sensitive workspace controls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import awf.api.routes.controls as controls_route
from awf.common.config import get_settings
from awf.db.models import Operation, WorkspaceEvent
from awf.db.session import make_session_factory

_BODY = {
    "repo_url": "git@github.com:example/controls.git",
    "branch_base": "main",
    "task_title": "Control a workspace",
    "task_prompt": "Exercise sensitive workspace controls.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create_workspace(client: AsyncClient) -> str:
    response = await client.post("/v1/workspaces", json=_BODY)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


def _auth(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    get_settings.cache_clear()
    return {"Authorization": "Bearer secret"}


async def _counts(engine: AsyncEngine, workspace_id: str) -> tuple[int, int]:
    factory = make_session_factory(engine)
    async with factory() as session:
        operation_count = await _count_rows(
            session, select(func.count()).select_from(Operation).where(
                Operation.workspace_id == workspace_id
            )
        )
        event_count = await _count_rows(
            session, select(func.count()).select_from(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == workspace_id
            )
        )
    return operation_count, event_count


async def _count_rows(session: AsyncSession, statement: Any) -> int:
    return int((await session.execute(statement)).scalar_one())


class FakeCleaner:
    calls: list[dict[str, object]] = []

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        remove_volumes: bool,
        remove_worktree: bool,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
    ) -> list[str]:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "repo_url": repo_url,
                "compose_project_name": compose_project_name,
                "compose_file_path": compose_file_path,
                "worktree_host_path": worktree_host_path,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            }
        )
        return []


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_sensitive_controls_require_idempotency_key(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_id = await _create_workspace(client)
    headers = _auth(monkeypatch)

    response = await _call_control(client, workspace_id, action, headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_REQUEST",
        "message": "Idempotency-Key header is required for this endpoint.",
    }


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_replay_same_key_returns_same_operation_without_duplicate_rows(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_id = await _create_workspace(client)
    stop_calls: list[str | None] = []

    async def fake_stop(compose_project_name: str | None) -> None:
        stop_calls.append(compose_project_name)

    monkeypatch.setattr(controls_route, "_stop_project", fake_stop)
    FakeCleaner.calls = []
    monkeypatch.setattr(controls_route, "_cleaner", FakeCleaner)
    headers = {**_auth(monkeypatch), "Idempotency-Key": f"{action}-same-key"}

    first = await _call_control(client, workspace_id, action, headers=headers)
    before_counts = await _counts(engine, workspace_id)
    replay = await _call_control(client, workspace_id, action, headers=headers)
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["operation_id"] == first.json()["operation_id"]
    assert after_counts == before_counts
    if action in {"cancel", "stop"}:
        assert len(stop_calls) == 1
    if action == "destroy":
        assert len(FakeCleaner.calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_same_key_with_different_payload_returns_idempotency_conflict(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_id = await _create_workspace(client)
    monkeypatch.setattr(controls_route, "_stop_project", _noop_stop)
    FakeCleaner.calls = []
    monkeypatch.setattr(controls_route, "_cleaner", FakeCleaner)
    headers = {**_auth(monkeypatch), "Idempotency-Key": f"{action}-conflict-key"}

    first = await _call_control(client, workspace_id, action, headers=headers)
    conflict = await _call_control(
        client,
        workspace_id,
        action,
        headers=headers,
        variant="different-payload",
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_stale_if_match_rejects_without_mutating(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_id = await _create_workspace(client)
    before_counts = await _counts(engine, workspace_id)
    stop_calls: list[str | None] = []

    async def fake_stop(compose_project_name: str | None) -> None:
        stop_calls.append(compose_project_name)

    monkeypatch.setattr(controls_route, "_stop_project", fake_stop)
    FakeCleaner.calls = []
    monkeypatch.setattr(controls_route, "_cleaner", FakeCleaner)
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": f"{action}-stale-version",
        "If-Match": "0",
    }

    response = await _call_control(client, workspace_id, action, headers=headers)
    after_counts = await _counts(engine, workspace_id)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error_code": "VERSION_CONFLICT",
        "message": "Workspace version does not match If-Match.",
        "detail": {"expected_version": 0, "actual_version": 1},
    }
    assert after_counts == before_counts
    assert stop_calls == []
    assert FakeCleaner.calls == []


async def _call_control(
    client: AsyncClient,
    workspace_id: str,
    action: str,
    *,
    headers: dict[str, str],
    variant: str = "base",
) -> Response:
    if action == "cancel":
        reason = "operator requested" if variant == "base" else "changed reason"
        return await client.post(
            f"/v1/workspaces/{workspace_id}/cancel",
            json={"reason": reason, "stop_stack": True},
            headers=headers,
        )
    if action == "stop":
        reason = "operator requested" if variant == "base" else "changed reason"
        return await client.post(
            f"/v1/workspaces/{workspace_id}/stop",
            json={"reason": reason},
            headers=headers,
        )
    if action == "destroy":
        remove_volumes = variant != "base"
        return await client.delete(
            f"/v1/workspaces/{workspace_id}",
            params={
                "force": True,
                "remove_volumes": remove_volumes,
                "remove_worktree": False,
            },
            headers=headers,
        )
    raise AssertionError(f"unknown action {action}")


async def _noop_stop(compose_project_name: str | None) -> None:
    return None
