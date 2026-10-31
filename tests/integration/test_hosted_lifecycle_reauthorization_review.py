"""Joined lifecycle review regressions: paused reauthorization and stable replay."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select

from awf.db.models import Operation, TaskAttempt, Workspace, WorkspaceEvent
from awf.hosted_node.schemas import Binding
from tests.integration.test_hosted_lifecycle_reauthorization import WS
from tests.integration.test_hosted_lifecycle_reauthorization_edges import control_case
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("action", ["guide", "remonitor"])
async def test_same_request_replay_after_monitor_head_advances(
    authority, create_client, tmp_path, action
):
    path, body = await control_case(authority, create_client, tmp_path, action)
    async with authority[2].begin() as session:
        row = await session.get(Workspace, WS)
        row.monitor_last_commit_sha = "a" * 40
    headers = {**create_client[1], "Idempotency-Key": "lost-then-monitor"}
    first = await create_client[0].post(path, json=body, headers=headers)
    assert first.status_code == 200, first.text
    async with authority[2].begin() as session:
        row = await session.get(Workspace, WS)
        row.monitor_last_commit_sha = "b" * 40
        row.base_commit = "c" * 40
    replay = await create_client[0].post(path, json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert first.json()["operation_id"] == replay.json()["operation_id"]
    async with authority[2]() as session:
        operation = await session.get(Operation, first.json()["operation_id"])
        assert operation.payload["source_head_sha"] == "a" * 40
        assert operation.payload["hosted_reauthorization"] == body["hosted_reauthorization"]
        assert (
            await session.scalar(
                select(func.count()).select_from(Operation).where(Operation.type == action)
            )
            == 1
        )


async def paused(authority, create_client, tmp_path, kind):
    action = "remonitor" if kind == "remonitor-human" else "guide"
    path, body = await control_case(authority, create_client, tmp_path, action)
    async with authority[2].begin() as session:
        row = await session.get(Workspace, WS)
        row.status = "monitoring_pr" if "human" in kind else "blocked"
        row.failure_reason = row.failure_message = None
        if "human" in kind:
            row.awaiting_human_since = datetime.now(UTC)
            row.awaiting_human_reason = "Operator decision required"
        else:
            row.pr_url = row.pr_number = None
            row.block_type = (
                "plan_conformance_decision" if kind == "conformance" else "protected_quality_gate"
            )
            row.block_epoch = 1
            row.block_reason_code = "OPERATOR_DECISION_REQUIRED"
        attempt = await session.scalar(select(TaskAttempt).where(TaskAttempt.workspace_id == WS))
        attempt.status = row.status
        # A paused warm runtime is not terminalized to manufacture release proof.
        await session.execute(
            delete(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == WS,
                WorkspaceEvent.event_type.in_(
                    ["workspace.terminal_runtime_released", "workspace.pre_launch_failed"]
                ),
            )
        )
    for role, binding in body["hosted_reauthorization"]["expected_association"][
        "credentials"
    ].items():
        result = await authority[0].receiver.revoke(
            Binding.model_validate(
                {
                    **binding,
                    "idempotency_key": f"old-revoked-{role}",
                    "fence": 2,
                }
            )
        )
        assert result.status == "revoked"
    return path, body


@pytest.mark.parametrize("kind", ["guide-human", "remonitor-human", "protected", "conformance"])
async def test_paused_existing_work_fresh_authority_without_fake_terminal_state(
    authority, create_client, tmp_path, kind
):
    path, body = await paused(authority, create_client, tmp_path, kind)
    headers = {**create_client[1], "Idempotency-Key": "paused-decision"}
    response = await create_client[0].post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    async with authority[2]() as session:
        row = await session.get(Workspace, WS)
        assert row.status == ("monitoring_pr" if "human" in kind else "blocked")
        assert (
            row.task_policy["hosted_node"]
            == body["hosted_reauthorization"]["replacement_association"]
        )
        if "human" in kind:
            assert row.awaiting_human_since is None
        else:
            assert row.pending_operator_hint["directive"] == "Continue review"
    assert (await authority[0].workspace(WS)).environment["GH_TOKEN"] == "fresh-github"
    replay = await create_client[0].post(path, json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["operation_id"] == response.json()["operation_id"]


@pytest.mark.parametrize(
    "fault", ["execution-claim", "monitor-claim", "unknown-operation", "attention"]
)
async def test_paused_reauthorization_still_denies_producers_or_unknown_outcomes(
    authority, create_client, tmp_path, fault
):
    path, body = await paused(authority, create_client, tmp_path, "guide-human")
    async with authority[2].begin() as session:
        row = await session.get(Workspace, WS)
        if fault == "execution-claim":
            row.execution_claimed_by = "producer"
        elif fault == "monitor-claim":
            row.monitor_claimed_by = "monitor"
        elif fault == "attention":
            row.task_policy = {**row.task_policy, "hosted_recovery": {"disposition": "attention"}}
        else:
            session.add(
                Operation(
                    id="unknown-paid",
                    workspace_id=WS,
                    type="execute",
                    status="running",
                    payload={"external_outcome": "unknown"},
                )
            )
    denied = await create_client[0].post(
        path, json=body, headers={**create_client[1], "Idempotency-Key": "paused-denied"}
    )
    assert denied.status_code == 409, denied.text
    async with authority[2]() as session:
        row = await session.get(Workspace, WS)
        assert (
            row.task_policy["hosted_node"] == body["hosted_reauthorization"]["expected_association"]
        )
        assert row.pending_operator_hint is None


@pytest.mark.parametrize(
    "field",
    ["workspace_id", "target", "namespace", "preparation_id", "grant_id", "idempotency_key"],
)
async def test_reauthorization_wire_denies_scope_changes_and_reused_authority(
    authority, create_client, tmp_path, field
):
    path, body = await control_case(authority, create_client, tmp_path)
    old = body["hosted_reauthorization"]["expected_association"]
    new = body["hosted_reauthorization"]["replacement_association"]
    if field == "workspace_id":
        new["workspace_id"] = "ws_other"
    for role, binding in new["credentials"].items():
        if field in {"preparation_id", "grant_id", "idempotency_key"}:
            binding[field] = old["credentials"][role][field]
        elif field == "target":
            binding[field] += ".other"
        else:
            binding[field] = "ws_other" if field == "workspace_id" else "other"
    response = await create_client[0].post(
        path, json=body, headers={**create_client[1], "Idempotency-Key": "invalid-pair"}
    )
    assert response.status_code == 422, response.text


async def test_detail_returns_exact_preimage_not_latest_material_grant(
    authority, create_client, tmp_path
):
    _, body = await control_case(authority, create_client, tmp_path)
    before = deepcopy(body["hosted_reauthorization"]["expected_association"])
    detail = await create_client[0].get(
        f"/v1/workspaces/{WS}",
        headers={**create_client[1], "Authorization": "Bearer synthetic-operator"},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["task_policy"]["hosted_node"] == before
