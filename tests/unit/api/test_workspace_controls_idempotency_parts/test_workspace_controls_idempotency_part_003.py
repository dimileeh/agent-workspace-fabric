"""Recovery operation endpoint idempotency tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, Workspace, WorkspaceEvent
from awf.db.session import make_session_factory
from tests.unit.api.test_workspace_controls_idempotency_parts.test_workspace_controls_idempotency_part_001 import (
    _auth,
    _counts,
    _seed_monitoring_workspace,
)


@pytest.mark.unit
async def test_rebase_endpoint_returns_operation_response_and_replays_exact_key(
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
        headers={
            **_auth(monkeypatch),
            "Idempotency-Key": "rebase-first",
            "If-Match": "7",
        },
    )
    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers={
            **_auth(monkeypatch),
            "Idempotency-Key": "rebase-first",
            "If-Match": "7",
        },
    )
    fresh_key = await client.post(
        f"/v1/workspaces/{workspace_id}/rebase",
        json={"reason": "base branch advanced"},
        headers={**_auth(monkeypatch), "Idempotency-Key": "rebase-second"},
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    payload = first.json()
    assert replay.json()["id"] == payload["id"]
    assert fresh_key.status_code == 409
    assert fresh_key.json()["detail"] == {
        "error_code": "WORKSPACE_STATE_NOT_REBASEABLE",
        "message": "Workspace is not in a state eligible for rebase recovery.",
        "detail": {
            "status": WorkspaceStatus.ready.value,
            "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
        },
    }
    assert payload["workspace_id"] == workspace_id
    assert payload["type"] == "rebase"
    assert payload["status"] == "pending"
    assert payload["idempotency_key"] == "rebase-first"
    assert payload["owner"] == "operator_api"
    assert payload["source"] == "operator_api"
    assert payload["reason"] == "base branch advanced"
    assert payload["reason_code"] == "OPERATOR_REBASE"

    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        operations = (
            (await session.execute(select(Operation).where(Operation.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        candidate = (
            await session.execute(
                select(MergeCandidate).where(MergeCandidate.workspace_id == workspace_id)
            )
        ).scalar_one()
        rebase_event = (
            await session.execute(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == workspace_id,
                    WorkspaceEvent.event_type == "workspace.rebase_requested",
                )
            )
        ).scalar_one()

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert workspace.version == 8
    assert [operation.id for operation in operations] == [payload["id"]]
    assert operations[0].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "base branch advanced",
        "reason_code": "OPERATOR_REBASE",
        "requested_action": "rebase",
        "recovery_mode": "rebase_only",
        "candidate_id": candidate.id,
        "attempt_id": candidate.attempt_id,
        "task_id": candidate.task_id,
        "pr_number": 42,
        "pr_url": "https://github.com/example/remonitor/pull/42",
        "source_head_sha": "b" * 40,
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
        "expected_version": 7,
    }
    assert rebase_event.reason_code == "OPERATOR_REBASE"
    assert rebase_event.payload == {
        "source": "operator_api",
        "reason": "base branch advanced",
        "operation_id": payload["id"],
        "recovery_mode": "rebase_only",
        "candidate_id": candidate.id,
        "expected_version": 7,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("action", "first_status", "body", "changed_body"),
    [
        (
            "refresh",
            WorkspaceStatus.ready,
            {"reason": "stale policy"},
            {"reason": "different stale policy"},
        ),
        (
            "validate",
            WorkspaceStatus.monitoring_pr,
            {"reason": "rerun required validation", "requested_tier": 2},
            {"reason": "rerun required validation", "requested_tier": 3},
        ),
    ],
)
async def test_recovery_same_key_replay_and_conflicting_payloads(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    first_status: WorkspaceStatus,
    body: dict[str, object],
    changed_body: dict[str, object],
) -> None:
    workspace_id = await _seed_monitoring_workspace(engine, final_status=first_status)
    headers = {**_auth(monkeypatch), "Idempotency-Key": f"{action}-same-key"}

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json=body,
        headers=headers,
    )
    before_counts = await _counts(engine, workspace_id)
    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json=body,
        headers=headers,
    )
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json=changed_body,
        headers=headers,
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert after_counts == before_counts


@pytest.mark.unit
@pytest.mark.parametrize(
    ("action", "first_status", "if_match", "body"),
    [
        ("refresh", WorkspaceStatus.ready, "3", {"reason": "stale policy"}),
        (
            "validate",
            WorkspaceStatus.monitoring_pr,
            "7",
            {"reason": "rerun required validation", "requested_tier": 2},
        ),
    ],
)
async def test_recovery_fresh_key_stale_if_match_rejects_before_active_coalesce(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    first_status: WorkspaceStatus,
    if_match: str,
    body: dict[str, object],
) -> None:
    workspace_id = await _seed_monitoring_workspace(engine, final_status=first_status)
    headers = {
        **_auth(monkeypatch),
        "Idempotency-Key": f"{action}-original",
        "If-Match": if_match,
    }

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json=body,
        headers=headers,
    )
    if action == "refresh":
        factory = make_session_factory(engine)
        async with factory() as session:
            workspace = await session.get(Workspace, workspace_id)
            assert workspace is not None
            workspace.version += 1
            await session.commit()
    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json=body,
        headers=headers,
    )
    before_counts = await _counts(engine, workspace_id)
    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/{action}",
        json=body,
        headers={
            **_auth(monkeypatch),
            "Idempotency-Key": f"{action}-fresh-stale-version",
            "If-Match": if_match,
        },
    )
    after_counts = await _counts(engine, workspace_id)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "VERSION_CONFLICT"
    assert conflict.json()["detail"]["detail"] == {
        "expected_version": int(if_match),
        "actual_version": int(if_match) + 1,
    }
    assert after_counts == before_counts
