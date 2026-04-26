"""Strict idempotency and version checks for sensitive workspace controls."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import awf.api.routes.controls as controls_route
from awf.common.config import get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.models import Operation, Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory

_BODY = {
    "repo_url": "git@github.com:example/controls.git",
    "branch_base": "main",
    "task_title": "Control a workspace",
    "task_prompt": "Exercise sensitive workspace controls.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}
_ACTIVE_CLAIM_EXPIRES_AT = datetime(2026, 4, 26, 12, 30, tzinfo=UTC)
_ACTIVE_CLAIM_EXPIRES_AT_JSON = _ACTIVE_CLAIM_EXPIRES_AT.replace(
    tzinfo=None
).isoformat()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create_workspace(client: AsyncClient) -> str:
    response = await client.post("/v1/workspaces", json=_BODY)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _seed_monitoring_workspace(
    engine: AsyncEngine,
    *,
    with_pr_url: bool = True,
    final_status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
    with_active_claims: bool = False,
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/remonitor.git",
            branch_base="development",
            task_title="Remonitor a workspace",
            task_prompt="Recover the PR monitor.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.remote_push_branch = workspace.branch_name
        workspace.base_commit = "a" * 40
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = f"/tmp/awf/{workspace.id}/compose.yml"
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.pushing, reason_code="SEED")
        if with_pr_url:
            workspace.pr_url = "https://github.com/example/remonitor/pull/42"
            workspace.pr_number = 42
            workspace.monitor_last_commit_sha = "b" * 40
        if final_status == WorkspaceStatus.monitoring_pr:
            await repo.transition(
                workspace,
                to=WorkspaceStatus.monitoring_pr,
                reason_code="SEED",
            )
        elif final_status == WorkspaceStatus.completed:
            await repo.transition(
                workspace,
                to=WorkspaceStatus.completed,
                reason_code="SEED",
            )
        else:
            raise AssertionError(f"unsupported seed status {final_status}")

        if with_active_claims:
            workspace.monitor_claimed_by = "dead-monitor-worker"
            workspace.monitor_claim_expires_at = _ACTIVE_CLAIM_EXPIRES_AT
            workspace.execution_claimed_by = "dead-execution-worker"
            workspace.execution_claim_expires_at = _ACTIVE_CLAIM_EXPIRES_AT
        await session.commit()
        return workspace.id


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


def _fake_cleaner_factory(calls: list[dict[str, object]]) -> type:
    class FakeCleaner:
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

    return FakeCleaner


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
    cleaner_calls: list[dict[str, object]] = []
    monkeypatch.setattr(controls_route, "_cleaner", _fake_cleaner_factory(cleaner_calls))
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
        assert len(cleaner_calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_same_key_with_different_payload_returns_idempotency_conflict(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_id = await _create_workspace(client)
    monkeypatch.setattr(controls_route, "_stop_project", _noop_stop)
    cleaner_calls: list[dict[str, object]] = []
    monkeypatch.setattr(controls_route, "_cleaner", _fake_cleaner_factory(cleaner_calls))
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
async def test_same_key_with_different_if_match_returns_idempotency_conflict(
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
    cleaner_calls: list[dict[str, object]] = []
    monkeypatch.setattr(controls_route, "_cleaner", _fake_cleaner_factory(cleaner_calls))
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": f"{action}-if-match-conflict",
        "If-Match": "1",
    }

    first = await _call_control(client, workspace_id, action, headers=headers)
    before_counts = await _counts(engine, workspace_id)
    conflict = await _call_control(
        client,
        workspace_id,
        action,
        headers={**headers, "If-Match": "0"},
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert after_counts == before_counts
    if action in {"cancel", "stop"}:
        assert len(stop_calls) == 1
    if action == "destroy":
        assert len(cleaner_calls) == 1


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
    cleaner_calls: list[dict[str, object]] = []
    monkeypatch.setattr(controls_route, "_cleaner", _fake_cleaner_factory(cleaner_calls))
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
    assert cleaner_calls == []


@pytest.mark.unit
async def test_remonitor_requires_idempotency_key(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(engine)

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers=_auth(monkeypatch),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_REQUEST",
        "message": "Idempotency-Key header is required for this endpoint.",
    }


@pytest.mark.unit
async def test_remonitor_replay_same_key_returns_same_operation_without_duplicate_rows(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        with_active_claims=True,
    )
    headers = {**_auth(monkeypatch), "Idempotency-Key": "remonitor-same-key"}

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers=headers,
    )
    before_counts = await _counts(engine, workspace_id)
    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers=headers,
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["operation_id"] == first.json()["operation_id"]
    assert after_counts == before_counts


@pytest.mark.unit
async def test_remonitor_resets_only_claims_and_records_audit_rows(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        with_active_claims=True,
    )
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": "remonitor-claims",
        "If-Match": "7",
    }

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_id"] == workspace_id
    assert payload["status"] == WorkspaceStatus.monitoring_pr.value
    assert payload["message"] == "workspace PR monitor recovery requested"

    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        operation = await session.get(Operation, payload["operation_id"])
        events = (
            await session.execute(
                select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == workspace_id)
                .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
            )
        ).scalars().all()

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.version == 8
    assert workspace.pr_url == "https://github.com/example/remonitor/pull/42"
    assert workspace.branch_name == f"awf/{workspace_id}"
    assert workspace.remote_push_branch == workspace.branch_name
    assert workspace.monitor_claimed_by is None
    assert workspace.monitor_claim_expires_at is None
    assert workspace.execution_claimed_by is None
    assert workspace.execution_claim_expires_at is None
    assert operation is not None
    assert operation.type == "remonitor"
    assert operation.status == "succeeded"
    assert operation.idempotency_key == "remonitor-claims"
    assert operation.payload == {
        "reason": "operator recovery",
        "expected_version": 7,
    }
    assert operation.result == {
        "status": WorkspaceStatus.monitoring_pr.value,
        "claims_reset": {
            "monitor_claimed_by": "dead-monitor-worker",
            "monitor_claim_expires_at": _ACTIVE_CLAIM_EXPIRES_AT_JSON,
            "execution_claimed_by": "dead-execution-worker",
            "execution_claim_expires_at": _ACTIVE_CLAIM_EXPIRES_AT_JSON,
        },
    }
    remonitor_event = next(
        event for event in events if event.reason_code == "OPERATOR_REMONITOR"
    )
    assert remonitor_event.event_type == "workspace.remonitor_requested"
    assert remonitor_event.old_state == WorkspaceStatus.monitoring_pr.value
    assert remonitor_event.new_state == WorkspaceStatus.monitoring_pr.value
    assert remonitor_event.payload == {
        "reason": "operator recovery",
        "operation_id": payload["operation_id"],
        "claims_reset": {
            "monitor_claimed_by": "dead-monitor-worker",
            "monitor_claim_expires_at": _ACTIVE_CLAIM_EXPIRES_AT_JSON,
            "execution_claimed_by": "dead-execution-worker",
            "execution_claim_expires_at": _ACTIVE_CLAIM_EXPIRES_AT_JSON,
        },
    }


@pytest.mark.unit
async def test_remonitor_same_key_with_different_reason_returns_idempotency_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(engine)
    headers = {**_auth(monkeypatch), "Idempotency-Key": "remonitor-conflict"}

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers=headers,
    )
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "different recovery reason"},
        headers=headers,
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.unit
async def test_remonitor_stale_if_match_rejects_without_mutating(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(engine, with_active_claims=True)
    before_counts = await _counts(engine, workspace_id)
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": "remonitor-stale-version",
        "If-Match": "0",
    }

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers=headers,
    )
    after_counts = await _counts(engine, workspace_id)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error_code": "VERSION_CONFLICT",
        "message": "Workspace version does not match If-Match.",
        "detail": {"expected_version": 0, "actual_version": 7},
    }
    assert after_counts == before_counts

    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
    assert workspace is not None
    assert workspace.monitor_claimed_by == "dead-monitor-worker"
    assert workspace.execution_claimed_by == "dead-execution-worker"


@pytest.mark.unit
async def test_remonitor_rejects_missing_pr_url_with_structured_bad_request(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(engine, with_pr_url=False)
    headers = {**_auth(monkeypatch), "Idempotency-Key": "remonitor-missing-pr"}

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "WORKSPACE_PR_URL_REQUIRED",
        "message": "Workspace remonitor requires an existing PR URL.",
        "detail": {"status": WorkspaceStatus.monitoring_pr.value},
    }


@pytest.mark.unit
async def test_remonitor_rejects_incompatible_state_with_structured_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        final_status=WorkspaceStatus.completed,
    )
    headers = {**_auth(monkeypatch), "Idempotency-Key": "remonitor-completed"}

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error_code": "WORKSPACE_STATE_NOT_REMONITORABLE",
        "message": "Workspace is not in a state eligible for remonitor recovery.",
        "detail": {
            "status": WorkspaceStatus.completed.value,
            "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
        },
    }


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
