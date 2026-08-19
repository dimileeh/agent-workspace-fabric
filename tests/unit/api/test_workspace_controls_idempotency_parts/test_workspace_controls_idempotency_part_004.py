"""Terminal replay checks for sensitive workspace recovery controls."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import OperationStatus, WorkspaceStatus
from tests.unit.api.test_workspace_controls_idempotency_parts.test_workspace_controls_idempotency_part_001 import (
    _auth,
    _counts,
    _mark_operation_and_workspace_terminal,
    _seed_monitoring_workspace,
)


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
    headers = {**_auth(monkeypatch), "Idempotency-Key": f"{action}-terminal-replay"}
    first = await client.post(f"/v1/workspaces/{workspace_id}/{action}", json=body, headers=headers)
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
        f"/v1/workspaces/{workspace_id}/{action}", json=body, headers=headers
    )
    after_counts = await _counts(engine, workspace_id)
    assert replay.status_code == 202
    replay_payload = replay.json()
    assert replay_payload["id"] == original_payload["id"]
    assert replay_payload["status"] == terminal_operation_status.value
    assert after_counts == before_counts
