"""Strict idempotency and version checks for sensitive workspace controls."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import awf.api.routes.controls as controls_route
from awf.common.config import get_settings
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, Workspace, WorkspaceEvent
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.operator_hints import OPERATOR_HINT_STATE_KEY
from awf.runtime.pr_monitor_runner.helpers import (
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _non_check_reviewer_settle_done_key,
    _non_check_reviewer_settle_started_key,
)

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
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_sensitive_controls_require_idempotency_key(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_id = await _create_workspace(client, monkeypatch)
    headers = _auth(monkeypatch)

    response = await _call_control(client, workspace_id, action, headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_REQUEST",
        "message": "Idempotency-Key header is required for this endpoint.",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "action",
    ["cancel", "stop", "destroy", "remonitor", "refresh", "validate", "rebase"],
)
async def test_sensitive_controls_reject_idempotency_key_over_database_limit(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": "k" * 129,
    }

    if action in {"cancel", "stop", "destroy"}:
        response = await _call_control(client, "ws_missing", action, headers=headers)
    else:
        body: dict[str, object] = {"reason": "operator recovery"}
        if action == "validate":
            body["requested_tier"] = 2
        response = await client.post(
            f"/v1/workspaces/ws_missing/{action}",
            json=body,
            headers=headers,
        )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_REQUEST",
        "message": "Idempotency-Key header must be at most 128 characters.",
    }


@pytest.mark.unit
async def test_recovery_operation_accepts_idempotency_key_at_database_limit(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        final_status=WorkspaceStatus.ready,
    )
    key = "k" * 128

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/refresh",
        json={"reason": "stale policy"},
        headers={**_auth(monkeypatch), "Idempotency-Key": key},
    )

    assert response.status_code == 202
    assert response.json()["idempotency_key"] == key


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_replay_same_key_returns_same_operation_without_duplicate_rows(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_id = await _create_workspace(client, monkeypatch)
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
    first_payload = first.json()
    replay_payload = replay.json()
    assert replay_payload["operation_id"] == first_payload["operation_id"]
    assert first_payload["operation_status"] == OperationStatus.succeeded.value
    assert replay_payload["operation_status"] == OperationStatus.succeeded.value
    assert after_counts == before_counts
    # cancel/stop now run a full compose down through the cleaner (issue #588 /
    # #583), so the bare docker-stop project_stopper is never invoked.
    if action in {"cancel", "stop", "destroy"}:
        assert len(cleaner_calls) == 1
    assert stop_calls == []


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_same_key_with_different_payload_returns_idempotency_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_id = await _create_workspace(client, monkeypatch)
    stop_calls: list[str | None] = []

    async def fake_stop(compose_project_name: str | None) -> None:
        stop_calls.append(compose_project_name)

    monkeypatch.setattr(controls_route, "_stop_project", fake_stop)
    cleaner_calls: list[dict[str, object]] = []
    monkeypatch.setattr(controls_route, "_cleaner", _fake_cleaner_factory(cleaner_calls))
    headers = {**_auth(monkeypatch), "Idempotency-Key": f"{action}-conflict-key"}

    first = await _call_control(client, workspace_id, action, headers=headers)
    before_counts = await _counts(engine, workspace_id)
    conflict = await _call_control(
        client,
        workspace_id,
        action,
        headers=headers,
        variant="different-payload",
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert after_counts == before_counts
    # cancel/stop now run a full compose down through the cleaner (issue #588 /
    # #583), so the bare docker-stop project_stopper is never invoked.
    if action in {"cancel", "stop", "destroy"}:
        assert len(cleaner_calls) == 1
    assert stop_calls == []


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_same_key_with_different_if_match_returns_idempotency_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_id = await _create_workspace(client, monkeypatch)
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
    # cancel/stop now run a full compose down through the cleaner (issue #588 /
    # #583), so the bare docker-stop project_stopper is never invoked.
    if action in {"cancel", "stop", "destroy"}:
        assert len(cleaner_calls) == 1
    assert stop_calls == []


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_stale_if_match_rejects_without_mutating(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_id = await _create_workspace(client, monkeypatch)
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
    assert first.status_code == 200
    first_payload = first.json()
    await _mark_operation_and_workspace_terminal(
        engine,
        workspace_id=workspace_id,
        operation_id=first_payload["operation_id"],
        operation_status=OperationStatus.succeeded,
        workspace_status=WorkspaceStatus.completed,
    )
    before_counts = await _counts(engine, workspace_id)
    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers=headers,
    )
    after_counts = await _counts(engine, workspace_id)

    assert replay.status_code == 200
    replay_payload = replay.json()
    assert replay_payload["operation_id"] == first_payload["operation_id"]
    assert first_payload["operation_status"] == OperationStatus.succeeded.value
    assert replay_payload["operation_status"] == OperationStatus.succeeded.value
    assert replay_payload["status"] == WorkspaceStatus.completed.value
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
    assert payload["operation_status"] == OperationStatus.succeeded.value
    assert payload["message"] == "workspace PR monitor recovery requested"

    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        operation = await session.get(Operation, payload["operation_id"])
        events = (
            (
                await session.execute(
                    select(WorkspaceEvent)
                    .where(WorkspaceEvent.workspace_id == workspace_id)
                    .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
                )
            )
            .scalars()
            .all()
        )

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
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "operator recovery",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_action": "remonitor",
        "pr_number": 42,
        "pr_url": "https://github.com/example/remonitor/pull/42",
        "source_head_sha": "b" * 40,
        "source_base_sha": "a" * 40,
        "expected_version": 7,
    }
    assert operation.result == {
        "status": WorkspaceStatus.monitoring_pr.value,
        "pr_number": 42,
        "pr_url": "https://github.com/example/remonitor/pull/42",
        "source_head_sha": "b" * 40,
        "source_base_sha": "a" * 40,
        "claims_reset": {
            "monitor_claimed_by": "dead-monitor-worker",
            "monitor_claim_expires_at": _ACTIVE_CLAIM_EXPIRES_AT_JSON,
            "execution_claimed_by": "dead-execution-worker",
            "execution_claim_expires_at": _ACTIVE_CLAIM_EXPIRES_AT_JSON,
        },
    }
    remonitor_event = next(event for event in events if event.reason_code == "OPERATOR_REMONITOR")
    assert remonitor_event.event_type == "workspace.remonitor_requested"
    assert remonitor_event.old_state == WorkspaceStatus.monitoring_pr.value
    assert remonitor_event.new_state == WorkspaceStatus.monitoring_pr.value
    pending_hint = remonitor_event.payload["pending_operator_hint"]
    assert remonitor_event.payload == {
        "reason": "operator recovery",
        "operation_id": payload["operation_id"],
        "pending_operator_hint": {
            "reason": "operator recovery",
            "operation_id": payload["operation_id"],
            "reason_code": "OPERATOR_REMONITOR",
            "requested_at": pending_hint["requested_at"],
            "status": "pending",
        },
        "claims_reset": {
            "monitor_claimed_by": "dead-monitor-worker",
            "monitor_claim_expires_at": _ACTIVE_CLAIM_EXPIRES_AT_JSON,
            "execution_claimed_by": "dead-execution-worker",
            "execution_claim_expires_at": _ACTIVE_CLAIM_EXPIRES_AT_JSON,
        },
        "expected_version": 7,
    }
    datetime.fromisoformat(pending_hint["requested_at"])


@pytest.mark.unit
async def test_remonitor_past_settle_persists_operator_hint_and_warns(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(engine)
    hint = "the docs CTA URL 404s; correct URL is https://example.test/docs"
    head_sha = "b" * 40
    initial_done_key = _initial_review_grace_done_key(42)
    initial_started_key = _initial_review_grace_started_key(42)
    settle_done_key = _non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
    )
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = {
            initial_done_key: "elapsed",
            settle_done_key: "elapsed",
        }
        await session.commit()

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": hint},
        headers={**_auth(monkeypatch), "Idempotency-Key": "remonitor-past-settle"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["warnings"] == [
        {
            "warning_code": "REMONITOR_PAST_SETTLE",
            "message": (
                "Workspace is past reviewer-settle window; auto-merge is frozen "
                "until the operator hint is processed."
            ),
        }
    ]

    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        operation = await session.get(Operation, payload["operation_id"])
        event = (
            (
                await session.execute(
                    select(WorkspaceEvent)
                    .where(
                        WorkspaceEvent.workspace_id == workspace_id,
                        WorkspaceEvent.reason_code == "OPERATOR_REMONITOR",
                    )
                    .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )

    assert workspace is not None
    state = workspace.monitor_threads_addressed
    assert initial_done_key not in state
    assert settle_done_key not in state
    assert float(state[initial_started_key]) >= 1_000_000_000
    assert float(state[settle_started_key]) >= 1_000_000_000
    stored_hint = json.loads(state[OPERATOR_HINT_STATE_KEY])
    assert stored_hint["reason"] == hint
    assert stored_hint["status"] == "pending"
    assert stored_hint["operation_id"] == payload["operation_id"]
    assert stored_hint["reason_code"] == "OPERATOR_REMONITOR"
    assert operation is not None
    assert operation.result is not None
    assert operation.result["warnings"] == payload["warnings"]
    assert event is not None
    assert event.payload["pending_operator_hint"]["operation_id"] == payload["operation_id"]
    assert event.payload["pending_operator_hint"]["reason"] == hint
    assert event.payload["warnings"] == payload["warnings"]


@pytest.mark.unit
async def test_remonitor_reopens_failed_candidate_with_latest_head_when_monitor_sha_lags(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_monitor_head = "b" * 40
    latest_candidate_head = "c" * 40
    workspace_id = await _seed_monitoring_workspace(
        engine,
        final_status=WorkspaceStatus.failed,
        with_open_candidate=True,
    )
    initial_done_key = _initial_review_grace_done_key(42)
    latest_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=latest_candidate_head,
    )
    latest_settle_done_key = _non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=latest_candidate_head,
    )
    stale_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=stale_monitor_head,
    )
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        candidate = (
            await session.execute(
                select(MergeCandidate).where(MergeCandidate.workspace_id == workspace_id)
            )
        ).scalar_one()
        workspace.monitor_last_commit_sha = stale_monitor_head
        workspace.monitor_threads_addressed = {
            initial_done_key: "elapsed",
            latest_settle_done_key: "elapsed",
        }
        candidate.head_sha = latest_candidate_head
        candidate.status = "closed"
        candidate.close_reason = "MONITOR_FAILED"
        await session.commit()

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "reattach latest PR head"},
        headers={**_auth(monkeypatch), "Idempotency-Key": "remonitor-latest-head"},
    )

    assert response.status_code == 200
    assert response.json()["warnings"] == [
        {
            "warning_code": "REMONITOR_PAST_SETTLE",
            "message": (
                "Workspace is past reviewer-settle window; auto-merge is frozen "
                "until the operator hint is processed."
            ),
        }
    ]
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        candidate = (
            await session.execute(
                select(MergeCandidate).where(MergeCandidate.workspace_id == workspace_id)
            )
        ).scalar_one()

    assert candidate.status == "open"
    assert candidate.head_sha == latest_candidate_head
    state = workspace.monitor_threads_addressed
    assert latest_settle_done_key not in state
    assert stale_settle_started_key not in state
    assert float(state[latest_settle_started_key]) >= 1_000_000_000


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
    before_counts = await _counts(engine, workspace_id)
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "different recovery reason"},
        headers=headers,
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert after_counts == before_counts


@pytest.mark.unit
async def test_remonitor_same_key_with_different_if_match_returns_idempotency_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(engine)
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": "remonitor-if-match-conflict",
        "If-Match": "7",
    }

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers=headers,
    )
    before_counts = await _counts(engine, workspace_id)
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "operator recovery"},
        headers={**headers, "If-Match": "8"},
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert after_counts == before_counts


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
            "eligible_statuses": [
                WorkspaceStatus.monitoring_pr.value,
                WorkspaceStatus.failed.value,
            ],
        },
    }


@pytest.mark.unit
async def test_remonitor_failed_workspace_with_pr_reopens_candidate_for_worker(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        final_status=WorkspaceStatus.failed,
        with_active_claims=True,
    )
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.failure_reason = "infrastructure_failure"
        workspace.failure_message = "old monitor worker failed during rebase recovery"
        workspace.monitor_iter_count = 7
        await session.commit()
    headers = {**_auth(monkeypatch), "Idempotency-Key": "remonitor-failed-open-pr"}

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/remonitor",
        json={"reason": "reattach open PR"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == WorkspaceStatus.monitoring_pr.value
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        candidate = (
            await session.execute(
                select(MergeCandidate).where(MergeCandidate.workspace_id == workspace_id)
            )
        ).scalar_one()
        remonitor_event = next(
            event
            for event in (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == workspace_id,
                        WorkspaceEvent.event_type == "workspace.remonitor_requested",
                    )
                )
            ).scalars()
        )

    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.failure_reason is None
    assert workspace.failure_message is None
    assert workspace.monitor_iter_count == 0
    assert workspace.monitor_claimed_by is None
    assert workspace.execution_claimed_by is None
    assert candidate.status == "open"
    assert candidate.close_reason is None
    assert candidate.failed_or_cancelled is False
    assert remonitor_event.old_state == WorkspaceStatus.failed.value
    assert remonitor_event.new_state == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
@pytest.mark.parametrize(
    "final_status",
    [
        WorkspaceStatus.completed,
        WorkspaceStatus.cancelled,
    ],
)
async def test_remonitor_rejects_incompatible_state_before_missing_pr_url(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    final_status: WorkspaceStatus,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        with_pr_url=False,
        final_status=final_status,
    )
    headers = {**_auth(monkeypatch), "Idempotency-Key": f"remonitor-{final_status}"}

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
            "status": final_status.value,
            "eligible_statuses": [
                WorkspaceStatus.monitoring_pr.value,
                WorkspaceStatus.failed.value,
            ],
        },
    }


@pytest.mark.unit
@pytest.mark.parametrize("action", ["refresh", "validate", "rebase"])
async def test_recovery_operations_require_authorization(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    final_status = WorkspaceStatus.ready if action == "refresh" else WorkspaceStatus.monitoring_pr
    workspace_id = await _seed_monitoring_workspace(
        engine,
        final_status=final_status,
        with_open_candidate=action == "rebase",
    )
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    get_settings.cache_clear()

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json={"reason": "operator recovery"},
        headers={
            "Authorization": "Bearer wrong",
            "Idempotency-Key": f"{action}-auth",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
@pytest.mark.parametrize("action", ["refresh", "validate", "rebase"])
async def test_recovery_operations_require_idempotency_key(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    final_status = WorkspaceStatus.ready if action == "refresh" else WorkspaceStatus.monitoring_pr
    workspace_id = await _seed_monitoring_workspace(
        engine,
        final_status=final_status,
        with_open_candidate=action == "rebase",
    )

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json={"reason": "operator recovery"},
        headers=_auth(monkeypatch),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_REQUEST",
        "message": "Idempotency-Key header is required for this endpoint.",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    (
        "action",
        "first_status",
        "with_open_candidate",
        "terminal_operation_status",
        "terminal_workspace_status",
        "body",
    ),
    [
        (
            "refresh",
            WorkspaceStatus.ready,
            False,
            OperationStatus.succeeded,
            WorkspaceStatus.destroyed,
            {"reason": "stale policy"},
        ),
        (
            "validate",
            WorkspaceStatus.monitoring_pr,
            False,
            OperationStatus.failed,
            WorkspaceStatus.completed,
            {"reason": "rerun required validation", "requested_tier": 2},
        ),
        (
            "rebase",
            WorkspaceStatus.monitoring_pr,
            True,
            OperationStatus.succeeded,
            WorkspaceStatus.completed,
            {"reason": "base branch advanced"},
        ),
    ],
)
async def test_recovery_exact_key_replays_terminal_operation_after_workspace_moves(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    first_status: WorkspaceStatus,
    with_open_candidate: bool,
    terminal_operation_status: OperationStatus,
    terminal_workspace_status: WorkspaceStatus,
    body: dict[str, object],
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        final_status=first_status,
        with_open_candidate=with_open_candidate,
    )
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": f"{action}-terminal-replay",
    }

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json=body,
        headers=headers,
    )
    assert first.status_code == 202
    original_payload = first.json()
    await _mark_operation_and_workspace_terminal(
        engine,
        workspace_id=workspace_id,
        operation_id=original_payload["id"],
        operation_status=terminal_operation_status,
        workspace_status=terminal_workspace_status,
    )
    before_counts = await _counts(engine, workspace_id)

    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json=body,
        headers=headers,
    )
    after_counts = await _counts(engine, workspace_id)

    assert replay.status_code == 202
    replay_payload = replay.json()
    assert replay_payload["id"] == original_payload["id"]
    assert replay_payload["status"] == terminal_operation_status.value
    assert after_counts == before_counts


@pytest.mark.unit
async def test_refresh_endpoint_returns_operation_response_and_coalesces_active_request(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(
        engine,
        final_status=WorkspaceStatus.ready,
    )
    headers = {**_auth(monkeypatch), "Idempotency-Key": "refresh-first"}

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/refresh",
        json={"reason": "stale policy"},
        headers={**headers, "If-Match": "3"},
    )
    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/refresh",
        json={"reason": "stale policy"},
        headers={**_auth(monkeypatch), "Idempotency-Key": "refresh-second"},
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    payload = first.json()
    assert replay.json()["id"] == payload["id"]
    assert payload["workspace_id"] == workspace_id
    assert payload["type"] == "refresh"
    assert payload["status"] == "pending"
    assert payload["idempotency_key"] == "refresh-first"
    assert payload["owner"] == "operator_api"
    assert payload["source"] == "operator_api"
    assert payload["reason"] == "stale policy"
    assert payload["reason_code"] == "OPERATOR_REFRESH"
    assert payload["payload"] == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "stale policy",
        "reason_code": "OPERATOR_REFRESH",
        "requested_action": "refresh",
        "expected_version": 3,
    }

    factory = make_session_factory(engine)
    async with factory() as session:
        operations = (
            (await session.execute(select(Operation).where(Operation.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        refresh_event = (
            await session.execute(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == workspace_id,
                    WorkspaceEvent.event_type == "workspace.refresh_requested",
                )
            )
        ).scalar_one()

    assert [operation.id for operation in operations] == [payload["id"]]
    assert refresh_event.reason_code == "OPERATOR_REFRESH"
    assert refresh_event.payload == {
        "source": "operator_api",
        "reason": "stale policy",
        "operation_id": payload["id"],
        "expected_version": 3,
    }


@pytest.mark.unit
async def test_validate_endpoint_returns_operation_response_and_coalesces_active_request(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_monitoring_workspace(engine)

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/validate",
        json={"reason": "rerun required validation", "requested_tier": 2},
        headers={
            **_auth(monkeypatch),
            "Idempotency-Key": "validate-first",
            "If-Match": "7",
        },
    )
    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/validate",
        json={"reason": "rerun required validation", "requested_tier": 2},
        headers={**_auth(monkeypatch), "Idempotency-Key": "validate-second"},
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    payload = first.json()
    assert replay.json()["id"] == payload["id"]
    assert payload["workspace_id"] == workspace_id
    assert payload["type"] == "validate"
    assert payload["status"] == "pending"
    assert payload["owner"] == "operator_api"
    assert payload["source"] == "operator_api"
    assert payload["reason"] == "rerun required validation"
    assert payload["reason_code"] == "OPERATOR_VALIDATE"
    assert payload["payload"] == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "rerun required validation",
        "reason_code": "OPERATOR_VALIDATE",
        "requested_action": "validate",
        "recovery_mode": "validate_only",
        "requested_tier": 2,
        "expected_version": 7,
    }

    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        operations = (
            (await session.execute(select(Operation).where(Operation.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        validate_event = (
            await session.execute(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == workspace_id,
                    WorkspaceEvent.event_type == "workspace.validate_requested",
                )
            )
        ).scalar_one()

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert workspace.version == 8
    assert [operation.id for operation in operations] == [payload["id"]]
    assert validate_event.reason_code == "OPERATOR_VALIDATE"
    assert validate_event.payload == {
        "source": "operator_api",
        "reason": "rerun required validation",
        "operation_id": payload["id"],
        "recovery_mode": "validate_only",
        "requested_tier": 2,
        "expected_version": 7,
    }
