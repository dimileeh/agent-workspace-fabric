"""Console-ready observability and workspace-control API tests."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.controls as controls_route
from awf.api.app import configure_database, create_app
from awf.common.config import get_settings
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from tests.postgres import create_postgres_test_engine

_BODY = {
    "repo_url": "git@github.com:example/console.git",
    "branch_base": "main",
    "task_title": "Add console data",
    "task_prompt": "Expose useful workspace observability.",
    "task_external_id": "TICKET-123",
    "agent": "codex",
    "test_commands": ["pytest -q"],
    "preflight": {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": "observability API fixture",
    },
}
_REPOSITORY_BODY = {key: value for key, value in _BODY.items() if key != "preflight"}
_V2_POLICY_BODY = {
    "repo": {
        "url": "git@github.com:example/console.git",
        "base_branch": "main",
    },
    "task": {
        "title": "Add console data",
        "prompt": "Expose useful workspace observability.",
        "kind": "feature_branch_pr",
        "agent": "codex",
        "external_id": "TICKET-456",
        "task_class": "test_task",
        "owned_paths": ["src/awf/api/**", "tests/unit/api/**"],
    },
    "preflight": {
        "provider_readiness_override": True,
    },
    "workspace": {"profile_ref": "auto", "profile": None},
    "validation": {"commands": ["pytest -q"], "requested_tier": 1},
    "resources": {},
}


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create_workspace(client: AsyncClient, **overrides: object) -> str:
    response = await client.post("/v1/workspaces", json={**_BODY, **overrides})
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_policy_workspace(client: AsyncClient) -> str:
    response = await client.post("/v1/workspaces", json=_V2_POLICY_BODY)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _mutate_workspace(engine: AsyncEngine, workspace_id: str) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        workspace.branch_name = "codex/observe"
        workspace.compose_project_name = "awf_ws_observe"
        workspace.pr_url = "https://github.com/example/console/pull/1"
        await repo.add_event(
            workspace,
            event_type="workspace.phase_started",
            reason_code="RUNNING_TEST",
            payload={"phase": "validation"},
        )
        await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
        )
        await session.commit()


async def _dispatch_monitor_recovery(engine: AsyncEngine, workspace_id: str) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.monitoring_pr,
        ):
            await repo.transition(workspace, to=target, reason_code="TEST")
        operation_payload = {
            "owner": "pr_monitor",
            "source": "pr_monitor",
            "reason": "Owned paths overlap a fresher workspace.",
            "reason_code": "STALE_OVERLAP",
            "stale_reason": "STALE_OVERLAP",
            "requested_action": "rebase",
            "recovery_mode": "rebase_only",
        }
        await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            payload=operation_payload,
        )
        await repo.transition(
            workspace,
            to=WorkspaceStatus.ready,
            reason_code="RECOVERY_DISPATCH",
        )
        await repo.add_event(
            workspace,
            event_type="monitor.recovery_dispatched",
            reason_code="RECOVERY_DISPATCH",
            payload={
                "reason": "STALE_OVERLAP",
                "req_action": "rebase",
                "recovery_mode": "rebase_only",
            },
        )
        await session.commit()


def _auth(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    get_settings.cache_clear()
    return {"Authorization": "Bearer secret"}


def _make_sync_test_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    api_token: str | None = "secret",
) -> tuple[str, TestClient, AsyncEngine]:
    if api_token is None:
        monkeypatch.setenv("AWF_API_TOKEN", "")
    else:
        monkeypatch.setenv("AWF_API_TOKEN", api_token)
    get_settings.cache_clear()
    engine = asyncio.run(create_postgres_test_engine())
    factory = make_session_factory(engine)
    log_path = tmp_path / "agent.log"
    log_path.write_text("hello websocket\n", encoding="utf-8")

    async def seed() -> str:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url=str(_BODY["repo_url"]),
                branch_base=str(_BODY["branch_base"]),
                task_title=str(_BODY["task_title"]),
                task_prompt=str(_BODY["task_prompt"]),
                task_external_id=str(_BODY["task_external_id"]),
                agent=str(_BODY["agent"]),
                test_commands=[],
            )
            repo = WorkspaceLogStreamRepository(session)
            await repo.create_or_get(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(log_path),
            )
            await repo.append_metadata(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                byte_delta=log_path.stat().st_size,
                line_delta=1,
            )
            await session.commit()
            return workspace.id

    workspace_id = asyncio.run(seed())
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    return workspace_id, TestClient(app), engine


def _make_empty_sync_test_client(tmp_path: Path) -> tuple[TestClient, AsyncEngine]:
    del tmp_path
    engine = asyncio.run(create_postgres_test_engine())
    factory = make_session_factory(engine)
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    return TestClient(app), engine


async def _seed_websocket_workspace(engine: AsyncEngine, tmp_path: Path) -> str:
    factory = make_session_factory(engine)
    log_path = tmp_path / "agent.log"
    log_path.write_text("hello websocket\n", encoding="utf-8")
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url=str(_BODY["repo_url"]),
            branch_base=str(_BODY["branch_base"]),
            task_title=str(_BODY["task_title"]),
            task_prompt=str(_BODY["task_prompt"]),
            task_external_id=str(_BODY["task_external_id"]),
            agent=str(_BODY["agent"]),
            test_commands=[],
        )
        repo = WorkspaceLogStreamRepository(session)
        await repo.create_or_get(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
            source="agent",
            name="Agent stdout",
            kind="stdout",
            path=str(log_path),
        )
        await repo.append_metadata(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
            byte_delta=log_path.stat().st_size,
            line_delta=1,
        )
        await session.commit()
        return workspace.id


@contextmanager
def _temporary_api_token(token: str | None) -> object:
    previous = os.environ.get("AWF_API_TOKEN")
    try:
        if token is None:
            os.environ["AWF_API_TOKEN"] = ""
        else:
            os.environ["AWF_API_TOKEN"] = token
        get_settings.cache_clear()
        yield
    finally:
        if previous is None:
            os.environ.pop("AWF_API_TOKEN", None)
        else:
            os.environ["AWF_API_TOKEN"] = previous
        get_settings.cache_clear()


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.event_sent = asyncio.Event()
        self.events: list[str] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        if payload.get("type") != "event":
            return
        event = payload["event"]
        assert isinstance(event, dict)
        event_type = event["event_type"]
        assert isinstance(event_type, str)
        self.events.append(event_type)
        self.event_sent.set()


class _FrameRecordingWebSocket:
    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []
        self.closed_codes: list[int] = []
        self._frame_event = asyncio.Event()

    async def send_json(self, payload: dict[str, object]) -> None:
        self.frames.append(payload)
        self._frame_event.set()

    async def close(self, *, code: int) -> None:
        self.closed_codes.append(code)

    async def wait_for_type(self, frame_type: str) -> None:
        while not any(frame.get("type") == frame_type for frame in self.frames):
            self._frame_event.clear()
            await self._frame_event.wait()

    async def wait_for_source(self, source: str) -> None:
        while not any(frame.get("source") == source for frame in self.frames):
            self._frame_event.clear()
            await self._frame_event.wait()


class TestOperationsAndControls:
    @pytest.mark.unit
    async def test_stop_records_operation_and_cancels_active_workspace(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        await _mutate_workspace(engine, workspace_id)
        stopped: list[str | None] = []
        cleaned: list[dict[str, object]] = []

        async def fake_stop(compose_project_name: str | None) -> None:
            stopped.append(compose_project_name)

        class FakeCleaner:
            async def cleanup(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                companion_worktrees: tuple[tuple[str, str], ...] = (),
                remove_volumes: bool = True,
                remove_worktree: bool = True,
                compose_project_name: str | None = None,
                compose_file_path: Path | None = None,
                worktree_host_path: Path | None = None,
            ) -> list[str]:
                _ = companion_worktrees
                cleaned.append(
                    {
                        "compose_project_name": compose_project_name,
                        "remove_volumes": remove_volumes,
                        "remove_worktree": remove_worktree,
                    }
                )
                return []

        monkeypatch.setattr(controls_route, "_stop_project", fake_stop)
        monkeypatch.setattr(controls_route, "_cleaner", FakeCleaner)
        headers = {**_auth(monkeypatch), "Idempotency-Key": "stop-observability"}

        response = await client.post(
            f"/v1/workspaces/{workspace_id}/stop",
            json={"reason": "operator requested", "stop_stack": True},
            headers=headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "cancelled"
        # stop now runs a full compose down (containers + network + port freed),
        # never a bare docker stop (issue #588 / #583).
        assert stopped == []
        assert cleaned == [
            {
                "compose_project_name": "awf_ws_observe",
                "remove_volumes": True,
                "remove_worktree": False,
            }
        ]

        operation = await client.get(f"/v1/operations/{body['operation_id']}", headers=headers)
        assert operation.status_code == 200
        assert operation.json()["type"] == "stop"
        assert operation.json()["status"] == "succeeded"

        operations = await client.get(f"/v1/workspaces/{workspace_id}/operations", headers=headers)
        assert operations.status_code == 200
        assert [item["type"] for item in operations.json()["items"]] == ["stop", "validate"]

    @pytest.mark.unit
    async def test_destroy_rejects_active_workspace_without_force(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        headers = {
            **_auth(monkeypatch),
            "Idempotency-Key": "destroy-active-without-force",
        }

        response = await client.delete(f"/v1/workspaces/{workspace_id}", headers=headers)

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "WORKSPACE_ACTIVE"

    @pytest.mark.unit
    async def test_force_destroy_runs_cleanup_and_marks_destroyed(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        calls: list[dict[str, object]] = []

        class FakeCleaner:
            async def cleanup(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                companion_worktrees: tuple[tuple[str, str], ...] = (),
                remove_volumes: bool = True,
                remove_worktree: bool = True,
                compose_project_name: str | None = None,
                compose_file_path: Path | None = None,
                worktree_host_path: Path | None = None,
            ) -> list[str]:
                _ = companion_worktrees
                calls.append(
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

        monkeypatch.setattr(controls_route, "_cleaner", FakeCleaner)
        headers = {**_auth(monkeypatch), "Idempotency-Key": "force-destroy-cleanup"}

        response = await client.delete(
            f"/v1/workspaces/{workspace_id}",
            params={"force": True, "remove_volumes": False, "remove_worktree": False},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["status"] == "destroyed"
        assert calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": _BODY["repo_url"],
                "compose_project_name": None,
                "compose_file_path": None,
                "worktree_host_path": None,
                "remove_volumes": False,
                "remove_worktree": False,
            }
        ]
