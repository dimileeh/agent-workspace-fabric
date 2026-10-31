"""Hosted lifecycle authority is not a whitelist or a recovery-attention escape."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from awf.db.models import Operation, OperatorGrantAuditRecord, Workspace
from awf.hosted_node.lifecycle_store import locked
from awf.hosted_node.schemas import Binding
from tests.integration.test_hosted_lifecycle_parity import fresh_association, source_workspace
from tests.unit.api.test_pr_monitor_adoption import _metadata
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("action", ["guide", "remonitor", "retry", "adopt-pr"])
@pytest.mark.parametrize(
    "denial",
    ["operator", "identity", "attention", "maintenance", "quarantine", "revoked", "namespace"],
)
async def test_lifecycle_fences(
    authority, create_client, create_application, tmp_path, action, denial
):
    runtime, value, factory = authority
    await source_workspace(
        authority,
        create_client,
        tmp_path,
        status="failed" if action == "retry" else "monitoring_pr",
    )
    client, headers = create_client
    headers = {**headers, "Idempotency-Key": "fenced"}
    body = {
        "reason": "decision",
        "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
    }
    if action == "guide":
        body["directive"] = "Continue existing work"
    path = f"/v1/workspaces/ws_cloud_stable/{action}"
    if action in {"retry", "adopt-pr"}:
        body = {
            "hosted": await fresh_association(
                runtime, value, namespace="foreign" if denial == "namespace" else "tenant"
            )
        }
        if action == "adopt-pr":
            path = "/v1/workspaces/adopt-pr"
            body.update(
                pr_url="https://github.com/example/repo/pull/42",
                source_workspace_id="ws_cloud_stable",
            )
            create_application[0].state.pr_adoption_metadata_fetcher = AsyncMock(
                return_value=_metadata(number=42)
            )
    if denial == "operator":
        headers["Authorization"] = "Bearer synthetic-operator"
    elif denial == "identity":
        headers["X-AWF-Expected-Boot-Epoch"] = "999"
    elif denial == "revoked":
        raw = (body.get("hosted") or value["hosted"])["credentials"]["agent"]
        await runtime.receiver.revoke(
            Binding.model_validate({**raw, "idempotency_key": "revoke", "fence": 2})
        )
    elif denial == "namespace" and action in {"guide", "remonitor"}:
        body["hosted_authorization"]["namespace"] = "foreign"
    else:
        async with factory.begin() as session:
            if denial == "attention":
                source = await session.get(Workspace, "ws_cloud_stable")
                source.task_policy = {
                    **source.task_policy,
                    "hosted_recovery": {"disposition": "attention"},
                }
            elif denial in {"maintenance", "quarantine"}:
                gate = await locked(session, runtime.receiver.identity)
                if denial == "maintenance":
                    gate.maintenance_generation = 7
                else:
                    gate.restore_quarantine = {"state": "pending"}
    response = await client.post(path, json=body, headers=headers)
    permitted_continuation = denial == "maintenance" and action in {"guide", "remonitor"}
    assert (
        response.status_code == 200
        if permitted_continuation
        else response.status_code in {403, 409, 503}
    ), response.text
    async with factory() as session:
        assert await session.get(Workspace, "ws_successor") is None
        operations = list(
            await session.scalars(
                select(Operation).where(
                    Operation.type.in_(["guide", "remonitor", "retry", "adopt_pr"])
                )
            )
        )
        assert len(operations) == int(permitted_continuation)


@pytest.mark.parametrize("authorization", [None, False, True])
async def test_guide_grants_require_explicit_authorized_actor(
    authority, create_client, tmp_path, authorization
):
    await source_workspace(authority, create_client, tmp_path, status="blocked")
    async with authority[2].begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.block_type = "protected_quality_gate"
        row.block_reason_code = "QUALITY_GATE_POLICY_CHANGED"
        row.block_epoch = 1
        row.block_violations = [
            {"path": "pyproject.toml", "section": "tool.coverage", "reason": "weakened", "line": 5}
        ]
    body = {
        "grants": ["pyproject.toml"],
        "reason": "Explicit caller decision",
        "operator": "caller",
        "approve_policy_downgrade": True,
    }
    if authorization is not None:
        body["hosted_authorization"] = {
            "namespace": "tenant",
            "actor": "caller",
            "allow_grants": authorization,
        }
    client, headers = create_client
    response = await client.post(
        "/v1/workspaces/ws_cloud_stable/guide",
        json=body,
        headers={**headers, "Idempotency-Key": "grant"},
    )
    assert response.status_code == (200 if authorization is True else 403), response.text
    async with authority[2]() as session:
        grants = list(await session.scalars(select(OperatorGrantAuditRecord)))
        assert len(grants) == int(authorization is True)
        if grants:
            assert grants[0].operator == "caller"


@pytest.mark.parametrize("action", ["guide", "remonitor"])
async def test_control_replay_cannot_change_attested_actor(
    authority, create_client, tmp_path, action
):
    await source_workspace(authority, create_client, tmp_path)
    client, headers = create_client
    headers = {**headers, "Idempotency-Key": "same-command"}
    body = {"reason": "decision", "hosted_authorization": {"namespace": "tenant", "actor": "first"}}
    if action == "guide":
        body["directive"] = "Continue review"
    first = await client.post(
        f"/v1/workspaces/ws_cloud_stable/{action}", json=body, headers=headers
    )
    assert first.status_code == 200, first.text
    body["hosted_authorization"]["actor"] = "different"
    changed = await client.post(
        f"/v1/workspaces/ws_cloud_stable/{action}", json=body, headers=headers
    )
    assert changed.status_code == 409, changed.text


async def test_retry_never_inherits_or_accepts_expired_source_material(
    authority, create_client, tmp_path
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    client, headers = create_client
    headers = {**headers, "Idempotency-Key": "fresh-required"}
    absent = await client.post("/v1/workspaces/ws_cloud_stable/retry", headers=headers)
    assert absent.status_code == 422, absent.text
    inherited = await client.post(
        "/v1/workspaces/ws_cloud_stable/retry",
        json={"hosted": authority[1]["hosted"]},
        headers=headers,
    )
    assert inherited.status_code == 409, inherited.text
    fresh = await fresh_association(authority[0], authority[1])
    expired = deepcopy(fresh)
    for binding in expired["credentials"].values():
        binding["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    response = await client.post(
        "/v1/workspaces/ws_cloud_stable/retry", json={"hosted": expired}, headers=headers
    )
    assert response.status_code in {403, 409, 503}, response.text
