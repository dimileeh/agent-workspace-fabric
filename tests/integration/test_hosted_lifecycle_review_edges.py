"""No alternate adoption path or late expiry may reopen unresolved work."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from awf.db.models import Operation, Workspace
from awf.db.repositories import OperationRepository
from tests.integration.test_hosted_lifecycle_parity import fresh_association, source_workspace
from tests.integration.test_hosted_lifecycle_successors import adoption
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("same_pr", [True, False])
async def test_static_adoption_cannot_bypass_existing_attention(
    authority, create_client, create_application, tmp_path, same_pr
):
    await source_workspace(authority, create_client, tmp_path)
    async with authority[2].begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.task_policy = {**row.task_policy, "hosted_recovery": {"disposition": "attention"}}
        if not same_pr:
            row.pr_number = 99
    body = await adoption(create_application, authority)
    response = await create_client[0].post(
        "/v1/workspaces/adopt-pr", json=body, headers=create_client[1]
    )
    assert response.status_code == (409 if same_pr else 202), response.text


@pytest.mark.parametrize("action", ["guide", "remonitor", "retry"])
async def test_expiry_before_commit_rolls_back_all_lifecycle_effects(
    authority, create_client, tmp_path, monkeypatch, action
):
    from awf.hosted_node import lifecycle_operations

    await source_workspace(
        authority,
        create_client,
        tmp_path,
        status="failed" if action == "retry" else "monitoring_pr",
    )
    body = {
        "reason": "decision",
        "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
    }
    if action == "guide":
        body["directive"] = "Continue review"
    elif action == "retry":
        body = {"hosted": await fresh_association(authority[0], authority[1])}
    finish = OperationRepository.finish

    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz or UTC) + timedelta(hours=1)

    async def expire_after_effect(self, operation, **kwargs):
        result = await finish(self, operation, **kwargs)
        monkeypatch.setattr(lifecycle_operations, "datetime", Expired)
        return result

    monkeypatch.setattr(OperationRepository, "finish", expire_after_effect)
    response = await create_client[0].post(
        f"/v1/workspaces/ws_cloud_stable/{action}",
        json=body,
        headers={**create_client[1], "Idempotency-Key": "expiry"},
    )
    assert response.status_code == 503, response.text
    async with authority[2]() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Operation).where(Operation.type == action)
            )
            == 0
        )
        assert await session.get(Workspace, "ws_successor") is None
