"""Console-ready observability and workspace-control API tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.service.workspaces as workspace_service
from awf.common.config import get_settings
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot

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


async def _block_workspace(engine: AsyncEngine, workspace_id: str, *, blocked_at: datetime) -> None:
    """Drive a workspace into ``blocked`` with a known ``blocked_at``, then append a
    later non-``blocked`` event so the overview age must come from ``blocked_at``
    rather than the trailing ``last_event``/``updated_at`` heuristic."""
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.blocked.value
        workspace.blocked_at = blocked_at
        await repo.add_event(
            workspace,
            event_type="workspace.note",
            reason_code="TEST",
            payload={"note": "trailing event while blocked"},
        )
        await session.commit()


async def _flag_awaiting_human(
    engine: AsyncEngine,
    workspace_id: str,
    *,
    since: datetime,
    reason: str,
) -> None:
    """Drive a workspace into ``monitoring_pr`` and stamp the awaiting-human flag."""
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
        workspace.awaiting_human_since = since
        workspace.awaiting_human_reason = reason
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


class TestConsoleViews:
    @pytest.mark.unit
    async def test_task_list_maps_workspace_rows(self, client: AsyncClient) -> None:
        workspace_id = await _create_workspace(client)

        response = await client.get("/v1/tasks")

        assert response.status_code == 200
        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert body["items"][0]["task_id"] == "TICKET-123"
        assert body["items"][0]["workspace_id"] == workspace_id
        assert body["items"][0]["repo_url"] == _BODY["repo_url"]
        assert body["items"][0]["agent"] == "codex"

    @pytest.mark.unit
    async def test_console_views_expose_policy_metadata(
        self,
        client: AsyncClient,
    ) -> None:
        workspace_id = await _create_policy_workspace(client)

        tasks = await client.get("/v1/tasks")
        overview = await client.get("/v1/workspaces/overview")

        assert tasks.status_code == 200
        task = tasks.json()["items"][0]
        assert task["task_id"] == "TICKET-456"
        assert task["workspace_id"] == workspace_id
        assert task["task_class"] == "test_task"
        assert task["owned_paths"] == ["src/awf/api/**", "tests/unit/api/**"]

        assert overview.status_code == 200
        item = overview.json()["items"][0]
        assert item["workspace_id"] == workspace_id
        assert item["task_class"] == "test_task"
        assert item["owned_paths"] == ["src/awf/api/**", "tests/unit/api/**"]

    @pytest.mark.unit
    async def test_workspace_overview_exposes_last_event_and_active_operation(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client)
        await _mutate_workspace(engine, workspace_id)

        response = await client.get("/v1/workspaces/overview")

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["workspace_id"] == workspace_id
        assert item["task_id"] == "TICKET-123"
        assert item["branch_name"] == "codex/observe"
        assert item["pr_url"] == "https://github.com/example/console/pull/1"
        assert item["current_phase"] == "requested"
        assert item["active_operation"] == "validate"
        assert item["last_event"]["event_type"] == "workspace.phase_started"
        assert item["recovery"] is None
        # Not blocked: the authoritative pause start is omitted.
        assert item["blocked_at"] is None

    @pytest.mark.unit
    async def test_workspace_overview_surfaces_blocked_at_over_trailing_event(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client)
        blocked_at = datetime(2026, 6, 18, 9, 0, tzinfo=UTC)
        await _block_workspace(engine, workspace_id, blocked_at=blocked_at)

        response = await client.get("/v1/workspaces/overview")

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["workspace_id"] == workspace_id
        assert item["status"] == "blocked"
        # The list now carries the authoritative pause start, so the "Blocked for"
        # age is derived from it rather than the trailing non-blocked last_event.
        assert item["last_event"]["event_type"] == "workspace.note"
        assert datetime.fromisoformat(item["blocked_at"]) == blocked_at

    @pytest.mark.unit
    async def test_workspace_overview_surfaces_awaiting_human_attention(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client)
        since = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        await _flag_awaiting_human(
            engine,
            workspace_id,
            since=since,
            reason="blocking review requires a human",
        )

        response = await client.get("/v1/workspaces/overview")

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["workspace_id"] == workspace_id
        assert item["status"] == "monitoring_pr"
        assert item["attention_required"] is True
        assert datetime.fromisoformat(item["awaiting_human_since"]) == since
        assert item["awaiting_human_reason"] == "blocking review requires a human"

    @pytest.mark.unit
    async def test_workspace_detail_surfaces_awaiting_human_attention(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client)
        since = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        await _flag_awaiting_human(
            engine,
            workspace_id,
            since=since,
            reason="merge BLOCKED by branch protection",
        )

        response = await client.get(f"/v1/workspaces/{workspace_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["attention_required"] is True
        assert datetime.fromisoformat(body["awaiting_human_since"]) == since
        assert body["awaiting_human_reason"] == "merge BLOCKED by branch protection"

    @pytest.mark.unit
    async def test_workspace_overview_omits_attention_when_not_flagged(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        # A monitoring_pr workspace that has not escalated carries no attention.
        workspace_id = await _create_workspace(client)
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
            await session.commit()

        response = await client.get("/v1/workspaces/overview")

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["status"] == "monitoring_pr"
        assert item["attention_required"] is False
        assert item["awaiting_human_since"] is None
        assert item["awaiting_human_reason"] is None

    @pytest.mark.unit
    async def test_workspace_detail_and_overview_expose_monitor_recovery_summary(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client)
        await _dispatch_monitor_recovery(engine, workspace_id)

        detail = await client.get(f"/v1/workspaces/{workspace_id}")
        overview = await client.get("/v1/workspaces/overview")

        assert detail.status_code == 200
        assert overview.status_code == 200
        for recovery in (
            detail.json()["recovery"],
            overview.json()["items"][0]["recovery"],
        ):
            assert recovery["from_state"] == "monitoring_pr"
            assert recovery["to_state"] == "ready"
            assert recovery["reason_code"] == "STALE_OVERLAP"
            assert recovery["action"] == "rebase"
            assert recovery["recovery_mode"] == "rebase_only"
            assert recovery["current_operation"]["type"] == "validate"
            assert recovery["current_operation"]["status"] == "pending"
            assert "monitoring_pr -> ready" in recovery["summary"]

        overview_item = overview.json()["items"][0]
        assert overview_item["active_operation"] == "validate"
        assert overview_item["last_event"]["event_type"] == "monitor.recovery_dispatched"

    @pytest.mark.unit
    async def test_ordinary_workspace_detail_returns_null_recovery(
        self,
        client: AsyncClient,
    ) -> None:
        workspace_id = await _create_workspace(client)

        response = await client.get(f"/v1/workspaces/{workspace_id}")

        assert response.status_code == 200
        assert response.json()["recovery"] is None

    @pytest.mark.unit
    async def test_runtime_endpoint_returns_mocked_container_snapshot(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        await _mutate_workspace(engine, workspace_id)

        class FakeRuntimeInspector:
            async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
                assert compose_project_name == "awf_ws_observe"
                return RuntimeSnapshot(
                    stack_state="running",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="abc123",
                            image="awf-agent-runtime:latest",
                            state="running",
                            status="Up 1 minute",
                            health="healthy",
                            ports=["127.0.0.1:8000->8000/tcp"],
                            started_at="2026-04-25T10:00:00Z",
                        )
                    ],
                )

        monkeypatch.setattr(workspace_service, "RuntimeInspector", FakeRuntimeInspector)

        response = await client.get(f"/v1/workspaces/{workspace_id}/runtime")

        assert response.status_code == 200
        body = response.json()
        assert body["stack_state"] == "running"
        assert body["compose_project_name"] == "awf_ws_observe"
        assert body["services"][0]["name"] == "agent"
        assert body["services"][0]["health"] == "healthy"

    @pytest.mark.unit
    async def test_runtime_endpoint_returns_404_for_unknown_workspace(
        self,
        client: AsyncClient,
    ) -> None:
        response = await client.get("/v1/workspaces/ws_missing/runtime")

        assert response.status_code == 404
        assert response.json()["detail"] == {
            "error_code": "NOT_FOUND",
            "message": "No workspace with id ws_missing",
        }


class TestLogs:
    @pytest.mark.unit
    async def test_log_endpoints_require_token_when_configured(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        _auth(monkeypatch)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/logs",
            headers={"Authorization": "Bearer wrong"},
        )

        assert response.status_code == 401
        assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"

    @pytest.mark.unit
    async def test_list_and_read_log_streams_with_offsets(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        log_path = tmp_path / "agent.log"
        log_path.write_text("alpha\nbeta\n", encoding="utf-8")
        factory = make_session_factory(engine)
        async with factory() as session:
            repo = WorkspaceLogStreamRepository(session)
            await repo.create_or_get(
                workspace_id=workspace_id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(log_path),
            )
            await repo.append_metadata(
                workspace_id=workspace_id,
                stream_id="agent.stdout",
                byte_delta=log_path.stat().st_size,
                line_delta=2,
            )
            await session.commit()

        headers = _auth(monkeypatch)

        listed = await client.get(f"/v1/workspaces/{workspace_id}/logs", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["items"][0]["stream_id"] == "agent.stdout"
        assert listed.json()["items"][0]["byte_count"] == len("alpha\nbeta\n")
        assert listed.json()["limit"] == 1

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/logs/agent.stdout",
            params={"offset": 6, "limit_bytes": 4},
            headers=headers,
        )
        assert read.status_code == 200
        assert read.json() == {
            "stream_id": "agent.stdout",
            "offset": 6,
            "next_offset": 10,
            "eof": False,
            "data": "beta",
        }

    @pytest.mark.unit
    async def test_missing_log_stream_returns_404(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        headers = _auth(monkeypatch)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/logs/nope",
            headers=headers,
        )

        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "NOT_FOUND"

    @pytest.mark.unit
    async def test_logs_for_missing_workspace_return_404(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        headers = _auth(monkeypatch)

        listed = await client.get("/v1/workspaces/ws_missing/logs", headers=headers)
        read = await client.get(
            "/v1/workspaces/ws_missing/logs/agent.stdout",
            headers=headers,
        )

        assert listed.status_code == 404
        assert listed.json()["detail"]["error_code"] == "NOT_FOUND"
        assert read.status_code == 404
        assert read.json()["detail"]["error_code"] == "NOT_FOUND"

    @pytest.mark.unit
    async def test_missing_log_file_returns_structured_404(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        missing_log_path = tmp_path / "deleted.log"
        factory = make_session_factory(engine)
        async with factory() as session:
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace_id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(missing_log_path),
            )
            await session.commit()

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/logs/agent.stdout",
            headers=_auth(monkeypatch),
        )

        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "LOG_FILE_MISSING"

    @pytest.mark.unit
    async def test_log_read_reports_eof_at_next_offset(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        log_path = tmp_path / "agent.log"
        log_path.write_text("alpha\n", encoding="utf-8")
        factory = make_session_factory(engine)
        async with factory() as session:
            repo = WorkspaceLogStreamRepository(session)
            await repo.create_or_get(
                workspace_id=workspace_id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(log_path),
            )
            await repo.append_metadata(
                workspace_id=workspace_id,
                stream_id="agent.stdout",
                byte_delta=log_path.stat().st_size,
                line_delta=1,
            )
            await session.commit()

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/logs/agent.stdout",
            params={"offset": 0, "limit_bytes": 10},
            headers=_auth(monkeypatch),
        )

        assert response.status_code == 200
        assert response.json()["data"] == "alpha\n"
        assert response.json()["next_offset"] == 6
        assert response.json()["eof"] is True
