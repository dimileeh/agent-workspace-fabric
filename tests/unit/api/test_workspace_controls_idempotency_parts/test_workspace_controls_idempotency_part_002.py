"""Strict idempotency and version checks for sensitive workspace controls."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import awf.api.routes.controls as controls_route
from awf.api.schemas import WorkspaceOperationRequest
from awf.common.config import get_settings
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.models import Operation, Workspace, WorkspaceEvent
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory

_BODY = {
    "repo_url": "git@github.com:example/controls.git",
    "branch_base": "main",
    "task_title": "Control a workspace",
    "task_prompt": "Exercise sensitive workspace controls.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
    "preflight": {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": "control idempotency fixture",
    },
}
_ACTIVE_CLAIM_EXPIRES_AT = datetime(2026, 4, 26, 12, 30, tzinfo=UTC)
_ACTIVE_CLAIM_EXPIRES_AT_JSON = _ACTIVE_CLAIM_EXPIRES_AT.isoformat()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create_workspace(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> str:
    headers = _auth(monkeypatch)
    response = await client.post("/v1/workspaces", json=_BODY, headers=headers)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _seed_monitoring_workspace(
    engine: AsyncEngine,
    *,
    with_pr_url: bool = True,
    with_open_candidate: bool = False,
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
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.remote_push_branch = workspace.branch_name
        workspace.base_commit = "a" * 40
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = f"/tmp/awf/{workspace.id}/compose.yml"
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        if final_status == WorkspaceStatus.ready:
            await session.commit()
            return workspace.id
        if final_status == WorkspaceStatus.destroying:
            await repo.transition(
                workspace,
                to=WorkspaceStatus.destroying,
                reason_code="SEED",
            )
            await session.commit()
            return workspace.id
        if final_status == WorkspaceStatus.destroyed:
            await repo.transition(
                workspace,
                to=WorkspaceStatus.destroying,
                reason_code="SEED",
            )
            await repo.transition(
                workspace,
                to=WorkspaceStatus.destroyed,
                reason_code="SEED",
            )
            await session.commit()
            return workspace.id
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.pushing, reason_code="SEED")
        if with_pr_url:
            workspace.pr_url = "https://github.com/example/remonitor/pull/42"
            workspace.pr_number = 42
            workspace.monitor_last_commit_sha = "b" * 40
        await repo.transition(
            workspace,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="SEED",
        )
        if final_status in {
            WorkspaceStatus.completed,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
        }:
            await repo.transition(
                workspace,
                to=final_status,
                reason_code="SEED",
            )
        elif final_status != WorkspaceStatus.monitoring_pr:
            raise AssertionError(f"unsupported seed status {final_status}")

        if with_open_candidate:
            if not workspace.pr_url:
                raise AssertionError("open candidate seed requires a PR URL")
            await MergeCandidateRepository(session).create_or_update_open_for_attempt(
                task=task,
                attempt=attempt,
                workspace=workspace,
                head_sha=workspace.monitor_last_commit_sha,
                base_sha=workspace.base_commit,
            )

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
            session,
            select(func.count())
            .select_from(Operation)
            .where(Operation.workspace_id == workspace_id),
        )
        event_count = await _count_rows(
            session,
            select(func.count())
            .select_from(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == workspace_id),
        )
    return operation_count, event_count


async def _mark_operation_and_workspace_terminal(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    operation_id: str,
    operation_status: OperationStatus,
    workspace_status: WorkspaceStatus,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        operation = await session.get(Operation, operation_id)
        workspace = await session.get(Workspace, workspace_id)
        assert operation is not None
        assert workspace is not None
        operation.status = operation_status.value
        operation.result = {"status": workspace_status.value}
        operation.finished_at = datetime.now(UTC)
        workspace.status = workspace_status.value
        await session.commit()


async def _count_rows(session: AsyncSession, statement: Any) -> int:
    return int((await session.execute(statement)).scalar_one())


def _fake_cleaner_factory(calls: list[dict[str, object]]) -> type:
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

    return FakeCleaner


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


def _operation_row(operation_id: str, operation_type: str) -> Operation:
    return Operation(
        id=operation_id,
        workspace_id="ws_direct",
        type=operation_type,
        status="pending",
        payload={"source": "test"},
        result=None,
        idempotency_key=f"{operation_type}-direct",
        created_at=datetime.now(UTC),
    )


@pytest.mark.unit
@pytest.mark.parametrize("action", ["refresh", "validate"])
async def test_recovery_same_key_with_different_if_match_returns_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    final_status = WorkspaceStatus.ready if action == "refresh" else WorkspaceStatus.monitoring_pr
    if_match = "3" if action == "refresh" else "7"
    workspace_id = await _seed_monitoring_workspace(engine, final_status=final_status)
    body = (
        {"reason": "stale policy"}
        if action == "refresh"
        else {"reason": "rerun required validation", "requested_tier": 2}
    )
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": f"{action}-if-match-conflict",
        "If-Match": if_match,
    }

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json=body,
        headers=headers,
    )
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json=body,
        headers={**headers, "If-Match": str(int(if_match) + 1)},
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.unit
async def test_validate_same_key_with_different_tier_returns_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(engine)
    headers = {**_auth(monkeypatch), "Idempotency-Key": "validate-tier-conflict"}

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/validate",
        json={"reason": "rerun required validation", "requested_tier": 2},
        headers=headers,
    )
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/validate",
        json={"reason": "rerun required validation", "requested_tier": 3},
        headers=headers,
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.unit
async def test_rebase_same_key_with_different_reason_returns_idempotency_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        with_open_candidate=True,
    )
    headers = {**_auth(monkeypatch), "Idempotency-Key": "rebase-reason-conflict"}

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers=headers,
    )
    before_counts = await _counts(engine, workspace_id)
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "different base branch reason"},
        headers=headers,
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert after_counts == before_counts


@pytest.mark.unit
async def test_rebase_same_key_with_different_if_match_returns_idempotency_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        with_open_candidate=True,
    )
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": "rebase-if-match-conflict",
        "If-Match": "7",
    }

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers=headers,
    )
    before_counts = await _counts(engine, workspace_id)
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers={**headers, "If-Match": "8"},
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert after_counts == before_counts


@pytest.mark.unit
async def test_rebase_fresh_key_with_different_reason_rejects_active_rebase_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        with_open_candidate=True,
    )

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers={**_auth(monkeypatch), "Idempotency-Key": "rebase-original"},
    )
    before_counts = await _counts(engine, workspace_id)
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "different base branch reason"},
        headers={**_auth(monkeypatch), "Idempotency-Key": "rebase-conflicting"},
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "WORKSPACE_REBASE_CONFLICT"
    assert after_counts == before_counts


@pytest.mark.unit
async def test_rebase_fresh_key_stale_if_match_rejects_before_active_coalesce(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        with_open_candidate=True,
    )
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": "rebase-original-version",
        "If-Match": "7",
    }

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers=headers,
    )
    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers=headers,
    )
    before_counts = await _counts(engine, workspace_id)
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers={
            **_auth(monkeypatch),
            "Idempotency-Key": "rebase-fresh-stale-version",
            "If-Match": "7",
        },
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "VERSION_CONFLICT"
    assert conflict.json()["detail"]["detail"] == {
        "expected_version": 7,
        "actual_version": 8,
    }
    assert after_counts == before_counts


@pytest.mark.unit
@pytest.mark.parametrize("action", ["refresh", "validate", "rebase"])
async def test_recovery_operations_missing_workspace_return_not_found(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    response = await client.post(
        f"/v1/workspaces/ws_missing/{action}",
        json={"reason": "operator recovery"},
        headers={**_auth(monkeypatch), "Idempotency-Key": f"{action}-missing"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "error_code": "NOT_FOUND",
        "message": "No workspace with id ws_missing",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("with_pr_url", "expected_status", "expected_code"),
    [
        (False, 400, "WORKSPACE_PR_URL_REQUIRED"),
        (True, 404, "MERGE_CANDIDATE_NOT_FOUND"),
    ],
)
async def test_rebase_rejects_missing_pr_or_candidate_without_operation(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    with_pr_url: bool,
    expected_status: int,
    expected_code: str,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        with_pr_url=with_pr_url,
    )
    if with_pr_url:
        factory = make_session_factory(engine)
        async with factory() as session:
            await MergeCandidateRepository(session).close_open_for_workspace(
                workspace_id,
                close_reason="TEST_MISSING_CANDIDATE",
            )
            await session.commit()
    before_counts = await _counts(engine, workspace_id)

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers={**_auth(monkeypatch), "Idempotency-Key": f"rebase-missing-{with_pr_url}"},
    )
    after_counts = await _counts(engine, workspace_id)

    assert response.status_code == expected_status
    assert response.json()["detail"]["error_code"] == expected_code
    assert after_counts == before_counts


@pytest.mark.unit
async def test_rebase_rejects_active_destructive_operation_without_new_operation(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        with_open_candidate=True,
    )
    factory = make_session_factory(engine)
    async with factory() as session:
        conflict = Operation(
            id="op_active_destroy",
            workspace_id=workspace_id,
            type="destroy",
            status="pending",
            payload={"source": "operator_api"},
        )
        session.add(conflict)
        await session.commit()
    before_counts = await _counts(engine, workspace_id)

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers={**_auth(monkeypatch), "Idempotency-Key": "rebase-destroy-conflict"},
    )
    after_counts = await _counts(engine, workspace_id)

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "WORKSPACE_OPERATION_CONFLICT"
    assert response.json()["detail"]["detail"] == {
        "operation_id": "op_active_destroy",
        "operation_type": "destroy",
        "operation_status": "pending",
    }
    assert after_counts == before_counts


@pytest.mark.unit
@pytest.mark.parametrize(
    ("action", "final_status", "error_code"),
    [
        ("refresh", WorkspaceStatus.destroying, "WORKSPACE_STATE_NOT_REFRESHABLE"),
        ("refresh", WorkspaceStatus.destroyed, "WORKSPACE_STATE_NOT_REFRESHABLE"),
        ("validate", WorkspaceStatus.completed, "WORKSPACE_STATE_NOT_VALIDATABLE"),
        ("validate", WorkspaceStatus.destroying, "WORKSPACE_STATE_NOT_VALIDATABLE"),
    ],
)
async def test_recovery_operations_reject_ineligible_states_without_operation(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    final_status: WorkspaceStatus,
    error_code: str,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        final_status=final_status,
    )
    before_counts = await _counts(engine, workspace_id)

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json={"reason": "operator recovery"},
        headers={**_auth(monkeypatch), "Idempotency-Key": f"{action}-{final_status}"},
    )
    after_counts = await _counts(engine, workspace_id)

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == error_code
    assert response.json()["detail"]["detail"]["status"] == final_status.value
    assert after_counts == before_counts


@pytest.mark.unit
async def test_recovery_route_handlers_return_operation_response_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh_operation = _operation_row("op_refresh_direct", "refresh")
    validate_operation = _operation_row("op_validate_direct", "validate")
    rebase_operation = _operation_row("op_rebase_direct", "rebase")

    class _Service:
        async def request_refresh_workspace(self, *_args: Any, **_kwargs: Any) -> Operation:
            return refresh_operation

        async def request_validate_workspace(self, *_args: Any, **_kwargs: Any) -> Operation:
            return validate_operation

        async def request_rebase_workspace(self, *_args: Any, **_kwargs: Any) -> Operation:
            return rebase_operation

    monkeypatch.setattr(controls_route, "_controls", lambda _session: _Service())

    refresh = await controls_route.refresh_workspace(
        "ws_direct",
        WorkspaceOperationRequest(reason="refresh"),
        idempotency_key="refresh-direct",
        if_match=None,
        session=None,  # type: ignore[arg-type]
    )
    validate = await controls_route.validate_workspace(
        "ws_direct",
        WorkspaceOperationRequest(reason="validate"),
        idempotency_key="validate-direct",
        if_match=None,
        session=None,  # type: ignore[arg-type]
    )
    rebase = await controls_route.rebase_workspace(
        "ws_direct",
        WorkspaceOperationRequest(reason="rebase"),
        idempotency_key="rebase-direct",
        if_match=None,
        session=None,  # type: ignore[arg-type]
    )

    assert refresh.id == "op_refresh_direct"
    assert refresh.type == "refresh"
    assert validate.id == "op_validate_direct"
    assert validate.type == "validate"
    assert rebase.id == "op_rebase_direct"
    assert rebase.type == "rebase"


@pytest.mark.unit
async def test_recovery_route_handlers_map_control_errors_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Service:
        async def request_refresh_workspace(self, *_args: Any, **_kwargs: Any) -> Operation:
            raise controls_route.WorkspaceNotFoundError("ws_direct")

        async def request_validate_workspace(self, *_args: Any, **_kwargs: Any) -> Operation:
            raise controls_route.WorkspaceNotFoundError("ws_direct")

        async def request_rebase_workspace(self, *_args: Any, **_kwargs: Any) -> Operation:
            raise controls_route.WorkspaceNotFoundError("ws_direct")

    monkeypatch.setattr(controls_route, "_controls", lambda _session: _Service())

    with pytest.raises(HTTPException) as refresh_error:
        await controls_route.refresh_workspace(
            "ws_direct",
            WorkspaceOperationRequest(reason="refresh"),
            idempotency_key="refresh-direct",
            if_match=None,
            session=None,  # type: ignore[arg-type]
        )
    with pytest.raises(HTTPException) as validate_error:
        await controls_route.validate_workspace(
            "ws_direct",
            WorkspaceOperationRequest(reason="validate"),
            idempotency_key="validate-direct",
            if_match=None,
            session=None,  # type: ignore[arg-type]
        )
    with pytest.raises(HTTPException) as rebase_error:
        await controls_route.rebase_workspace(
            "ws_direct",
            WorkspaceOperationRequest(reason="rebase"),
            idempotency_key="rebase-direct",
            if_match=None,
            session=None,  # type: ignore[arg-type]
        )

    assert refresh_error.value.status_code == 404
    assert validate_error.value.status_code == 404
    assert rebase_error.value.status_code == 404
