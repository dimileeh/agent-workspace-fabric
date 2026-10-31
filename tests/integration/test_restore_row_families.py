"""All guarded SQL families stay contained, including global/unattempted work."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    MergeCandidate,
    ResourceReservation,
    ServiceGCRequest,
    Task,
    TaskAttempt,
    ValidationRun,
    WorkspaceSecretLease,
)
from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES
from awf.hosted_node.models import Grant, PreparationRow
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_quarantine import restore


async def seed_rows(receiver):
    workspace = await create(receiver.factory)
    now = datetime.now(UTC)
    async with receiver.factory.begin() as session:
        for identifier in ("task", "no-attempt"):
            session.add(
                Task(
                    id=identifier,
                    repo_url="https://example.invalid/repo",
                    base_branch="main",
                    title="Retained",
                    prompt="Never execute",
                )
            )
        await session.flush()
        session.add(
            TaskAttempt(
                id="attempt",
                task_id="task",
                workspace_id=workspace.id,
                attempt_number=1,
                agent="codex",
                status="requested",
                title="Retained",
                repo_url="https://example.invalid/repo",
                base_branch="main",
            )
        )
        await session.flush()
        session.add(
            ValidationRun(
                id="validation",
                workspace_id=workspace.id,
                attempt_id="attempt",
                tier=1,
                command_set_hash="0" * 64,
                status="running",
            )
        )
        session.add(
            ResourceReservation(
                id="reservation",
                workspace_id=workspace.id,
                attempt_id="attempt",
                node_id=receiver.identity.owner,
                steady_cpu=1,
                steady_memory_gb=1,
                peak_cpu=1,
                peak_memory_gb=1,
                phase="requested",
            )
        )
        session.add(
            WorkspaceSecretLease(
                id="lease",
                workspace_id=workspace.id,
                attempt_id="attempt",
                secret_name="github",
                kind="mount",
                target="/run/secrets/github",
                mode="ro",
                provider="protected",
                status="mounted",
                issued_at=now,
            )
        )
        session.add(ServiceGCRequest(id="gc", node_id=None, status="pending", requested_at=now))
        session.add(
            MergeCandidate(
                id="merge",
                workspace_id=workspace.id,
                attempt_id="attempt",
                task_id="task",
                pr_url="https://example.invalid/pr/1",
                pr_number=1,
                repo_url="https://example.invalid/repo",
                base_branch="main",
                status="open",
            )
        )
        session.add(
            CallbackSubscription(
                id="subscription",
                name="no-delivery",
                target_url="https://example.invalid/callback",
                timeout_seconds=1,
                max_attempts=1,
                initial_backoff_seconds=1,
                idempotency_key="subscription",
                request_hash="hash",
            )
        )
        await session.flush()
        session.add(
            CallbackDelivery(
                id="callback",
                subscription_id="subscription",
                workspace_id=None,
                event_kind="workspace",
                event_type="workspace.created",
                source_id=workspace.id,
                dedupe_key="callback",
                idempotency_key="callback",
                max_attempts=1,
                status="pending",
            )
        )
        session.add(
            PreparationRow(
                id="old-preparation",
                owner=receiver.identity.owner,
                namespace="old",
                workspace_id=workspace.id,
                preparation_id="old",
                binding={
                    "identity": receiver.identity.model_dump(),
                    "namespace": "old",
                    "workspace_id": workspace.id,
                    "preparation_id": "old",
                    "purpose": "credential",
                    "target": "/run/awf/secrets/agent",
                    "expires_at": (now + timedelta(minutes=10)).isoformat(),
                },
            )
        )
        session.add(
            Grant(
                id="old-grant",
                preparation="old-preparation",
                grant_id="old",
                fence=1,
                revision=1,
                authorization_generation=1,
                current="old-receipt",
                pending=None,
            )
        )
    return workspace


async def test_restored_all_row_families_cannot_mutate_or_claim(ready_node):
    _, receiver, _ = ready_node
    await seed_rows(receiver)
    client, receiver, _, binding = await restore(ready_node)
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    assert response.status_code == 200, response.text
    async with receiver.factory() as session:
        quarantine = await session.scalar(
            text("SELECT restore_quarantine FROM hosted_node_lifecycle")
        )
        assert quarantine["nonterminal_counts"]["tasks"] == 2
        for table in (
            "task_attempts",
            "operations",
            "validation_runs",
            "callback_deliveries",
            "merge_candidates",
            "service_gc_requests",
            "resource_reservations",
            "workspace_secret_leases",
        ):
            assert quarantine["nonterminal_counts"][table] == 1
        assert await session.scalar(select(WorkspaceSecretLease.status)) == "revoked"
        grant = await session.get(Grant, "old-grant")
        assert grant.revoked and grant.current is None and grant.pending is None
    for table in GUARDED_TABLES:
        async with receiver.factory() as session:
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) > 0
        with pytest.raises(DBAPIError, match="RESTORE_QUARANTINED"):
            async with receiver.factory.begin() as session:
                await session.execute(text(f"UPDATE {table} SET id=id"))
