"""Exact-preimage, material, ownership and transaction edges of fresh controls."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.models import Operation, Workspace
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.models import Grant, Owner, PreparationRow, Receipt
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Binding
from tests.integration.test_hosted_lifecycle_reauthorization import (
    WS,
    install_pair,
    quiet_failed,
    wake,
)
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


async def control_case(authority, create_client, tmp_path, action="guide"):
    await quiet_failed(authority, create_client, tmp_path)
    expected = HostedWorkspace.model_validate(authority[1]["hosted"]).model_dump(mode="json")
    replacement = await install_pair(*create_client, expected, authority[0].receiver.identity)
    body = {
        "reason": "Current operator decision",
        "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
        "hosted_reauthorization": {
            "expected_association": expected,
            "replacement_association": replacement,
        },
        **({"directive": "Continue review"} if action == "guide" else {}),
    }
    return f"/v1/workspaces/{WS}/{action}", body


async def unchanged(factory, expected):
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.task_policy["hosted_node"] == expected
        assert row.pending_operator_hint is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.type.in_(["guide", "remonitor", "retry"]))
            )
            == 0
        )


@pytest.mark.parametrize(
    "fault",
    [
        "preimage",
        "foreign-tenant",
        "foreign-owner",
        "attention",
        "claim",
        "release-revoked",
        "completed",
        "version",
        "missing-pr",
        "revoke-new",
        "expired-new",
        "missing-file",
        "unacknowledged",
        "quarantine",
        "approval",
        "missing-authorization",
        "null",
        "unknown",
    ],
)
async def test_reauthorization_denials_do_not_mutate_source(
    authority, create_client, tmp_path, monkeypatch, fault
):
    runtime, _, factory = authority
    path, body = await control_case(authority, create_client, tmp_path)
    expected = deepcopy(body["hosted_reauthorization"]["expected_association"])
    replacement = body["hosted_reauthorization"]["replacement_association"]
    headers = {**create_client[1], "Idempotency-Key": "deny"}
    if fault == "preimage":
        body["hosted_reauthorization"]["expected_association"]["credentials"]["agent"]["fence"] += 1
    elif fault in {"foreign-tenant", "foreign-owner"}:
        for association in body["hosted_reauthorization"].values():
            for binding in association["credentials"].values():
                if fault == "foreign-tenant":
                    binding["namespace"] = "foreign"
                else:
                    binding["identity"]["owner"] = "foreign"
    elif fault == "version":
        headers["If-Match"] = "999"
    elif fault == "revoke-new":
        await runtime.receiver.revoke(
            Binding.model_validate(
                {
                    **replacement["credentials"]["agent"],
                    "idempotency_key": "new-revoke",
                    "fence": 2,
                }
            )
        )
    elif fault == "expired-new":
        from awf.hosted_node import runtime_authority

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz or UTC) + timedelta(hours=1)

        monkeypatch.setattr(runtime_authority, "datetime", Clock)
    elif fault == "missing-file":
        raw = replacement["credentials"]["agent"]
        async with factory() as session:
            prep = await runtime.receiver.preparation(session, Binding.model_validate(raw))
            receipt = await session.scalar(
                select(Receipt).where(Receipt.idempotency_key == raw["idempotency_key"])
            )
            runtime.receiver.store.remove(prep.id, receipt.id)
    elif fault == "approval":
        body.update(grants=["src/protected.py"], operator="caller")
    elif fault == "missing-authorization":
        body.pop("hosted_authorization")
    elif fault == "null":
        body["hosted_reauthorization"] = None
    elif fault == "unknown":
        body["hosted_reauthorization"]["allow"] = True
    else:
        async with factory.begin() as session:
            row = await session.get(Workspace, WS)
            if fault == "attention":
                row.task_policy = {
                    **row.task_policy,
                    "hosted_recovery": {"disposition": "attention"},
                }
            elif fault == "claim":
                row.monitor_claimed_by = "old-producer"
            elif fault == "release-revoked":
                await WorkspaceRepository(session).add_event(
                    row,
                    event_type="workspace.terminal_runtime_release_revoked",
                    reason_code="TERMINAL_RUNTIME_RELEASE_REVOKED_ORPHAN_STOP_FAILED",
                )
            elif fault == "completed":
                row.status = "completed"
            elif fault == "missing-pr":
                row.pr_url = None
            elif fault == "unacknowledged":
                owner = await session.get(Owner, runtime.receiver.identity.owner)
                owner.acknowledgement = None
            elif fault == "quarantine":
                gate = await session.get(NodeLifecycleRow, 1)
                gate.restore_quarantine = {"state": "pending"}
    response = await create_client[0].post(path, json=body, headers=headers)
    assert response.status_code in {400, 403, 409, 422, 503}, response.text
    await unchanged(factory, expected)


@pytest.mark.parametrize(
    "fault",
    [
        "missing-receipt",
        "receipt-binding",
        "preparation",
        "grant",
        "acceptance",
        "storage",
        "namespace",
        "physical",
        "generation",
        "future",
        "attention",
        "claim",
        "maintenance",
    ],
)
async def test_two_wake_history_does_not_widen_authority(
    authority, create_client, create_application, tmp_path, fault
):
    await quiet_failed(authority, create_client, tmp_path)
    runtime = None
    try:
        for _ in range(2):
            runtime = await wake(authority, create_application, create_client)
        target = await install_pair(
            *create_client, authority[1]["hosted"], runtime.receiver.identity, workspace_id="ws_new"
        )
        async with authority[2].begin() as session:
            row = await session.get(Workspace, WS)
            row.pr_url = row.pr_number = None
            if fault in {"namespace", "physical", "generation", "future"}:
                policy = deepcopy(row.task_policy)
                for binding in policy["hosted_node"]["credentials"].values():
                    if fault == "namespace":
                        binding["namespace"] = "foreign"
                    elif fault == "physical":
                        binding["identity"]["incarnation"] = "other"
                    elif fault == "generation":
                        binding["identity"]["recovery_generation"] += 1
                    else:
                        binding["identity"]["boot_epoch"] = runtime.receiver.identity.boot_epoch + 1
                row.task_policy = policy
            elif fault == "attention":
                row.task_policy = {
                    **row.task_policy,
                    "hosted_recovery": {"disposition": "attention"},
                }
            elif fault == "claim":
                row.execution_claimed_by = "producer"
            elif fault in {"acceptance", "storage", "maintenance"}:
                gate = await session.get(NodeLifecycleRow, 1)
                if fault == "acceptance":
                    gate.recovery_acceptance = None
                elif fault == "storage":
                    gate.database_ref = "other-db"
                else:
                    gate.maintenance_generation = 7
            else:
                receipt = await session.scalar(
                    select(Receipt).where(Receipt.idempotency_key == "agent")
                )
                if fault == "missing-receipt":
                    await session.delete(receipt)
                elif fault == "receipt-binding":
                    receipt.binding = {**receipt.binding, "namespace": "other"}
                else:
                    grant = await session.get(Grant, receipt.grant)
                    if fault == "grant":
                        grant.grant_id = "other"
                    else:
                        prep = await session.get(PreparationRow, grant.preparation)
                        prep.binding = {**prep.binding, "target": "/run/awf/secrets/other"}
        response = await create_client[0].post(
            f"/v1/workspaces/{WS}/retry",
            json={"hosted": target},
            headers={**create_client[1], "Idempotency-Key": "history-denied"},
        )
        assert response.status_code in {403, 409}, response.text
        async with authority[2]() as session:
            assert await session.get(Workspace, "ws_new") is None
    finally:
        if runtime is not None:
            await runtime.receiver.close()


@pytest.mark.parametrize("fault", ["revoke-after-read", "commit", "expiry-at-commit"])
async def test_material_commit_races_leave_original_association(
    authority, create_client, tmp_path, monkeypatch, fault
):
    runtime, _, factory = authority
    path, body = await control_case(authority, create_client, tmp_path)
    expected = body["hosted_reauthorization"]["expected_association"]
    binding = body["hosted_reauthorization"]["replacement_association"]["credentials"]["agent"]
    if fault == "revoke-after-read":
        original = runtime.materials

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            await runtime.receiver.revoke(
                Binding.model_validate(
                    {
                        **binding,
                        "idempotency_key": "race-revoke",
                        "fence": 2,
                    }
                )
            )
            return result

        monkeypatch.setattr(runtime, "materials", changed)
    elif fault == "commit":

        async def failed(_session):
            raise SQLAlchemyError("synthetic commit refusal")

        monkeypatch.setattr(AsyncSession, "commit", failed)
    else:
        from awf.hosted_node import lifecycle_operations

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz or UTC) + timedelta(hours=1)

        original = lifecycle_operations.commit_hosted

        async def expired(session):
            monkeypatch.setattr(lifecycle_operations, "datetime", Clock)
            await original(session)

        monkeypatch.setattr("awf.api.routes.controls.commit_hosted", expired)
    response = await create_client[0].post(
        path, json=body, headers={**create_client[1], "Idempotency-Key": "race"}
    )
    assert response.status_code in {500, 503}, response.text
    await unchanged(factory, expected)


@pytest.mark.parametrize("same_pair", [False, True])
async def test_concurrent_reauthorization_serializes_preimage_and_replay(
    authority, create_client, tmp_path, same_pair
):
    path, first = await control_case(authority, create_client, tmp_path)
    second = deepcopy(first)
    if not same_pair:
        second["hosted_reauthorization"]["replacement_association"] = await install_pair(
            *create_client,
            first["hosted_reauthorization"]["expected_association"],
            authority[0].receiver.identity,
            label="second",
        )
    responses = await asyncio.gather(
        *[
            create_client[0].post(
                path,
                json=body,
                headers={**create_client[1], "Idempotency-Key": "one" if same_pair else str(index)},
            )
            for index, body in enumerate((first, second))
        ]
    )
    assert sorted(r.status_code for r in responses) == ([200, 200] if same_pair else [200, 409]), [
        r.text for r in responses
    ]
    async with authority[2]() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Operation).where(Operation.type == "guide")
            )
            == 1
        )


@pytest.mark.parametrize("action", ["guide", "remonitor"])
async def test_reauthorization_lost_reply_is_one_committed_operation(
    authority, create_client, create_application, tmp_path, action
):
    path, body = await control_case(authority, create_client, tmp_path, action)

    async def lose(scope, receive, send):
        async def lost(message):
            if message["type"] == "http.response.start":
                raise ConnectionError("synthetic lost response")
            await send(message)

        await create_application[0](scope, receive, lost)

    headers = {**create_client[1], "Idempotency-Key": "lost"}
    async with AsyncClient(transport=ASGITransport(app=lose), base_url="http://test") as client:
        with pytest.raises(ConnectionError, match="lost response"):
            await client.post(path, json=body, headers=headers)
    async with authority[2]() as session:
        operation = await session.scalar(select(Operation).where(Operation.type == action))
        assert operation is not None
        operation_id = operation.id
    replay = await create_client[0].post(path, json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["operation_id"] == operation_id
    changed = deepcopy(body)
    changed["reason"] = "Changed intent"
    denied = await create_client[0].post(path, json=changed, headers=headers)
    assert denied.status_code == 409, denied.text
