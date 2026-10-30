"""Atomic all-work quarantine after rewind, never I5 queued/monitor resumption."""

from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from awf.db.models import Workspace
from awf.hosted_node.checkpoint_schemas import CHECKPOINT_SCHEMA_REVISION
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcceptance, RecoveryAcknowledgement
from awf.hosted_node.models import Grant
from awf.hosted_node.restore_schemas import RestoreBinding, RestoreBootEvidence
from awf.hosted_node.retained_work import MAX_RETAINED_WORKSPACES, RetainedWorkPlan, reconcile
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Identity


def evidence_fresh(expiry: datetime) -> bool:
    return expiry.tzinfo is not None and datetime.now(UTC) < expiry <= datetime.now(
        UTC
    ) + timedelta(seconds=60)


async def validate_restore_boot(
    session: AsyncSession, config: HostedSettings, evidence: RestoreBootEvidence
) -> RestoreBinding:
    binding = RestoreBinding.model_validate(evidence.binding.model_dump())
    gate = await session.get(NodeLifecycleRow, 1, with_for_update=True)
    if (
        not evidence_fresh(evidence.expires_at)
        or config.lifecycle is None
        or gate is None
        or binding.checkpoint.identity.owner != config.owner
        or binding.candidate_incarnation != config.incarnation
        or binding.candidate_recovery_generation != config.recovery_generation
        or binding.target_disk_ref != config.lifecycle.disk_ref
        or binding.target_database_ref != config.lifecycle.database_ref
        or gate.checkpoint_marker != binding.checkpoint.model_dump(mode="json")
        or gate.identity != binding.checkpoint.identity.model_dump()
        or gate.disk_ref != binding.checkpoint.disk_ref
        or gate.database_ref != binding.checkpoint.database_ref
        or gate.maintenance_generation != binding.checkpoint.maintenance_generation
        or gate.maintenance_released_through >= binding.checkpoint.maintenance_generation
        or gate.phase != "ready"
        or gate.recovery_key is None
        or gate.drain_generation is not None
        or gate.restore_quarantine is not None
    ):
        raise MaterialDeniedError("RESTORE_BOOT_EVIDENCE_UNAVAILABLE", 503)
    revisions = (
        (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    )
    if revisions != [CHECKPOINT_SCHEMA_REVISION]:
        raise MaterialDeniedError("RESTORE_SCHEMA_MISMATCH")
    return binding


# Counts are committed evidence, not execution status. The node latch covers all
# guarded families, including rows not associated with a resumable workspace.
NONTERMINAL = {
    "workspaces": "status NOT IN ('completed','failed','cancelled','destroyed')",
    "tasks": "NOT EXISTS(SELECT 1 FROM task_attempts a WHERE a.task_id=tasks.id) OR EXISTS(SELECT 1 FROM task_attempts a WHERE a.task_id=tasks.id AND a.status NOT IN ('completed','failed','cancelled','destroyed'))",
    "task_attempts": "status NOT IN ('completed','failed','cancelled','destroyed')",
    "operations": "status NOT IN ('succeeded','failed','cancelled')",
    "validation_runs": "status NOT IN ('succeeded','failed')",
    "callback_deliveries": "status NOT IN ('succeeded','failed','skipped')",
    "merge_candidates": "status NOT IN ('merged','closed')",
    "service_gc_requests": "status NOT IN ('completed','failed','expired')",
    "resource_reservations": "released_at IS NULL",
    "workspace_secret_leases": "status NOT IN ('revoked','released')",
}


async def accept_restore(
    session: AsyncSession, gate: NodeLifecycleRow, value: RecoveryAcknowledgement
) -> None:
    context = gate.restore_quarantine
    if context is None or context["state"] not in {"pending", "held"}:
        raise MaterialDeniedError("RESTORE_STATE_CONFLICT")
    if context["state"] == "held":
        # A process restart needs fresh exact restore/physical proof, but never
        # repeats classification or turns that proof into execution permission.
        if await session.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM workspaces WHERE "
                "status NOT IN ('completed','failed','cancelled','destroyed') AND "
                "coalesce(task_policy::jsonb #>> '{hosted_recovery,disposition}','') <> 'attention')"
            )
        ):
            raise MaterialDeniedError("RESTORE_INVENTORY_UNSUPPORTED")
        attention = await session.scalar(
            text(
                "SELECT count(*) FROM workspaces WHERE task_policy::jsonb #>> '{hosted_recovery,disposition}'='attention'"
            )
        )
        gate.recovery_acceptance = RecoveryAcceptance(
            **value.model_dump(),
            previous_identity=gate.previous_identity,
            disk_ref=gate.disk_ref,
            database_ref=gate.database_ref,
            queued_workspaces=0,
            monitor_workspaces=0,
            attention_workspaces=attention,
        ).model_dump(mode="json")
        await session.execute(update(Grant).values(revoked=True, current=None, pending=None))
        gate.phase = "ready"
        gate.recovery_key, gate.receipt_ref = value.idempotency_key, value.receipt_ref
        await session.flush()
        return
    identity = Identity.model_validate(gate.identity)
    counts = {
        table: await session.scalar(text(f"SELECT count(*) FROM {table} WHERE {predicate}"))
        for table, predicate in NONTERMINAL.items()
    }
    # Scope terminal history only when it still owns an active child or claim.
    children = [
        f"EXISTS(SELECT 1 FROM {table} c WHERE c.workspace_id=workspaces.id AND ({predicate}))"
        for table, predicate in NONTERMINAL.items()
        if table not in {"workspaces", "tasks", "service_gc_requests"}
    ]
    children.append(
        "EXISTS(SELECT 1 FROM callback_deliveries c WHERE c.status NOT IN ('succeeded','failed','skipped') "
        "AND (EXISTS(SELECT 1 FROM operations o WHERE o.id=c.operation_id AND o.workspace_id=workspaces.id) "
        "OR EXISTS(SELECT 1 FROM merge_candidates m WHERE m.id=c.merge_candidate_id AND m.workspace_id=workspaces.id) "
        "OR EXISTS(SELECT 1 FROM workspace_events e WHERE e.id=c.source_id AND e.workspace_id=workspaces.id)))"
    )
    rows = list(
        await session.scalars(
            select(Workspace)
            .options(raiseload("*"))
            .where(
                or_(
                    Workspace.status.not_in(("completed", "failed", "cancelled", "destroyed")),
                    Workspace.execution_claimed_by.is_not(None),
                    Workspace.monitor_claimed_by.is_not(None),
                    Workspace.task_policy["hosted_recovery"]["disposition"].as_string()
                    == "attention",
                    text("(" + " OR ".join(children) + ")"),
                )
            )
            .order_by(Workspace.id)
            .limit(MAX_RETAINED_WORKSPACES + 1)
            .with_for_update(of=Workspace)
        )
    )
    if len(rows) > MAX_RETAINED_WORKSPACES or any(
        row.node_id not in (None, identity.owner) for row in rows
    ):
        raise MaterialDeniedError("RESTORE_INVENTORY_UNSUPPORTED")
    for workspace in rows:
        policy = workspace.task_policy or {}
        if "hosted_node" in policy:
            try:
                hosted = HostedWorkspace.model_validate(policy["hosted_node"])
            except ValidationError:
                raise MaterialDeniedError("RESTORE_ASSOCIATION_INVALID") from None
            if (
                hosted.workspace_id != workspace.id
                or hosted.credentials.agent.identity.owner != identity.owner
            ):
                raise MaterialDeniedError("RESTORE_OWNER_MISMATCH")
    acceptance = RecoveryAcceptance(
        **value.model_dump(),
        previous_identity=gate.previous_identity,
        disk_ref=gate.disk_ref,
        database_ref=gate.database_ref,
        queued_workspaces=0,
        monitor_workspaces=0,
        attention_workspaces=len(rows),
    )
    await reconcile(
        session,
        gate,
        value,
        RetainedWorkPlan(acceptance, [(row, "attention", None) for row in rows]),
    )
    await session.execute(update(Grant).values(revoked=True, current=None, pending=None))
    gate.restore_quarantine = {
        **context,
        "state": "held",
        "identity": gate.identity,
        "receipt_ref": value.receipt_ref,
        "idempotency_key": value.idempotency_key,
        "nonterminal_counts": counts,
    }
    await session.flush()


async def deny_restore_materials(session: AsyncSession) -> None:
    gate = await session.get(NodeLifecycleRow, 1)
    if gate is not None and gate.restore_quarantine is not None:
        raise MaterialDeniedError("RESTORE_QUARANTINED")
