"""Retry service callers cannot bypass hosted authority or durable replay bindings."""

from copy import deepcopy

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.config import Settings
from awf.db.models import Workspace
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_authority import SESSION_KEY
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Binding
from awf.service.workspaces_retry import retry_workspace_row
from awf.service.workspaces_retry_errors import WorkspaceRetryIdempotencyConflictError
from tests.integration.test_hosted_lifecycle_migrated import engine as engine
from tests.integration.test_hosted_lifecycle_parity import fresh_association, source_workspace
from tests.integration.test_retained_work_acceptance import ACK, PREFIX
from tests.integration.test_retained_work_lock_conflicts import snapshot
from tests.integration.test_retained_work_timing import cohort
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import alternate_credentials
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize(
    "boundary", ["override", "configured_without_runtime", "stored_without_runtime"]
)
async def test_retry_service_rejects_authority_bypass_before_mutation(
    authority, create_client, tmp_path, boundary
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    runtime, value, factory = authority
    association = HostedWorkspace.model_validate(await fresh_association(runtime, value))
    before = await snapshot(factory)
    settings = Settings(
        _env_file=None,
        hosted_node=runtime.receiver.config if boundary != "stored_without_runtime" else None,
    )
    expected = (
        "HOSTED_CREDENTIAL_OVERRIDE_FORBIDDEN"
        if boundary == "override"
        else "HOSTED_RUNTIME_INTEGRATION_REQUIRED"
    )
    async with factory() as session:
        if boundary != "override":
            session.info.pop(SESSION_KEY)
        with pytest.raises(MaterialDeniedError, match=expected) as denied:
            await retry_workspace_row(
                session,
                "ws_cloud_stable",
                settings=settings,
                hosted=association if boundary == "override" else None,
                provider_readiness_override=boundary == "override",
                provider_readiness_override_reason="caller cannot replace hosted authority",
                idempotency_key="bypass-denied",
            )
        assert denied.value.status == (422 if boundary == "override" else 403)
        await session.rollback()
    assert await snapshot(factory) == before


@pytest.mark.parametrize("boundary", ["exact", "different_grant", "revoke_after_preflight"])
async def test_retry_service_durable_replay_requires_current_exact_association(
    authority, create_client, tmp_path, monkeypatch, boundary
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    runtime, value, factory = authority
    association = HostedWorkspace.model_validate(await fresh_association(runtime, value))
    client, headers = create_client
    accepted = await client.post(
        "/v1/workspaces/ws_cloud_stable/retry",
        json={"hosted": association.model_dump(mode="json")},
        headers={**headers, "Idempotency-Key": "durable-retry"},
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["new_workspace_id"] == "ws_successor"
    accepted_association = association.model_dump(mode="json")

    if boundary == "different_grant":
        alternate = await alternate_credentials(
            runtime, {**deepcopy(value), "hosted": association.model_dump(mode="json")}
        )
        association = HostedWorkspace.model_validate(alternate["hosted"])

    revoked_state = []
    if boundary == "revoke_after_preflight":
        original_materials = runtime.materials

        async def revoke_after_materials(*args, **kwargs):
            result = await original_materials(*args, **kwargs)
            binding = Binding.model_validate(association.credentials.agent.model_dump())
            await runtime.receiver.revoke(
                Binding.model_validate(
                    {
                        **binding.model_dump(),
                        "fence": binding.fence + 1,
                        "idempotency_key": "revoke-before-replay-lock",
                    }
                )
            )
            revoked_state.append(await snapshot(factory))
            return result

        monkeypatch.setattr(runtime, "materials", revoke_after_materials)

    before = await snapshot(factory)
    async with factory() as session:
        arguments = {
            "settings": Settings(_env_file=None, hosted_node=runtime.receiver.config),
            "hosted": association,
            "idempotency_key": "durable-retry",
        }
        if boundary == "exact":
            replay = await retry_workspace_row(session, "ws_cloud_stable", **arguments)
            assert replay.new_workspace.id == accepted.json()["new_workspace_id"]
            assert replay.operation.id == accepted.json()["operation_id"]
            assert replay.attempt_number == accepted.json()["attempt_number"]
        elif boundary == "different_grant":
            with pytest.raises(WorkspaceRetryIdempotencyConflictError):
                await retry_workspace_row(session, "ws_cloud_stable", **arguments)
        else:
            with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
                await retry_workspace_row(session, "ws_cloud_stable", **arguments)
        await session.rollback()
    assert await snapshot(factory) == (revoked_state[0] if revoked_state else before)
    if boundary == "revoke_after_preflight":
        assert len(revoked_state) == 1
    async with factory() as session:
        destination = await session.get(Workspace, "ws_successor")
        assert destination.task_policy["hosted_node"] == accepted_association


@pytest.mark.parametrize(
    "statement,sqlstate",
    [("SELECT 1/0", "22012"), ("SELECT * FROM absent_retry_inventory_table", "42P01")],
)
async def test_retained_inventory_preserves_non_lock_database_failures(
    authority, tmp_path, monkeypatch, statement, sqlstate
):
    async with cohort(authority, tmp_path, 2) as retained:
        client, _, factory, _, _, _ = retained
        before = await snapshot(factory)
        original_execute = AsyncSession.execute
        errors = []

        async def execute(session, query, *args, **kwargs):
            lock = getattr(query, "_for_update_arg", None)
            if (
                lock is not None
                and lock.nowait
                and any(
                    item.get("entity") is Workspace
                    for item in getattr(query, "column_descriptions", ())
                )
            ):
                try:
                    return await original_execute(session, text(statement))
                except DBAPIError as failure:
                    errors.append(failure.orig.sqlstate)
                    raise
            return await original_execute(session, query, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "execute", execute)
        response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
        assert errors == [sqlstate]
        assert response.status_code == 503, response.text
        assert response.json() == {"error_code": "AUTHORITY_UNAVAILABLE"}
        assert await snapshot(factory) == before
