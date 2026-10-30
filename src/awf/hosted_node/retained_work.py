"""Fixed retained-row reconciliation inside the final lifecycle acceptance.

The caller holds owner then lifecycle locks. No provider work, claims, external
I/O, cancellation or invented terminal outcomes belong in this transaction.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    MergeCandidate,
    Operation,
    ResourceReservation,
    ServiceGCRequest,
    Task,
    TaskAttempt,
    ValidationRun,
    Workspace,
    WorkspaceEvent,
    WorkspaceSecretLease,
)
from awf.db.repositories.base import terminal_runtime_effectively_released_expr
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcceptance, RecoveryAcknowledgement
from awf.hosted_node.lifecycle_store import database_quiescent
from awf.hosted_node.models import Grant, PreparationRow, Receipt
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Binding, Identity, Preparation

RECOVERY_POLICY_KEY = "hosted_recovery"
_TERMINAL = {"completed", "failed", "cancelled", "destroyed"}
_OP_TERMINAL = {"succeeded", "failed", "cancelled"}
MAX_RETAINED_WORKSPACES = 1024
MAX_ACTIVE_OPERATIONS_PER_WORKSPACE = 256
MAX_ACTIVE_PREPARATIONS = 4096


def _unresolved() -> NoReturn:
    raise MaterialDeniedError("RECOVERY_OUTCOME_UNRESOLVED")


def _passive_monitor_operation(operation: Operation, workspace: Workspace) -> bool:
    payload = operation.payload or {}
    return (
        isinstance(payload, dict)
        and operation.status in {"pending", "running"}
        and payload.get("owner") == payload.get("source") == "pr_monitor"
        and payload.get("pr_url") == workspace.pr_url
        and payload.get("pr_number") == workspace.pr_number
        and workspace.pr_number is not None
        and (operation.type, payload.get("action"), payload.get("requested_action"))
        in {
            ("human_wait", "human_wait", "notify_human"),
            ("monitor_state", "check_wait", "wait_for_ci"),
            ("monitor_state", "check_wait", "wait_for_rerun_rollup"),
            ("monitor_state", "check_wait", "wait_for_transient_ci"),
        }
    )


async def _fresh_association(
    session: AsyncSession,
    old: HostedWorkspace,
    identity: Identity,
) -> HostedWorkspace:
    selected: dict[str, Binding] = {}
    rows = (
        await session.execute(
            select(PreparationRow, Grant, Receipt)
            .join(Grant, Grant.preparation == PreparationRow.id)
            .join(Receipt, Receipt.id == Grant.current)
            .where(
                PreparationRow.owner == identity.owner,
                PreparationRow.workspace_id == old.workspace_id,
                Grant.revoked.is_(False),
                Grant.pending.is_(None),
                Receipt.state == "installed",
            )
            .limit(MAX_ACTIVE_PREPARATIONS + 1)
        )
    ).all()
    if len(rows) > MAX_ACTIVE_PREPARATIONS:
        raise MaterialDeniedError("RECOVERY_BINDINGS_REQUIRED")
    now = datetime.now(UTC)
    for role in ("agent", "github"):
        anchor = getattr(old.credentials, role)
        matches = []
        for preparation, grant, receipt in rows:
            binding = Binding.model_validate(receipt.binding)
            prepared = preparation.binding
            if binding.identity != identity or binding.target != anchor.target:
                continue
            if (
                receipt.owner != identity.owner
                or receipt.grant != grant.id
                or binding.namespace != anchor.namespace
                or binding.purpose != anchor.purpose
                or binding.workspace_id != old.workspace_id
                or binding.preparation_id != preparation.preparation_id
                or binding.grant_id != grant.grant_id
                or binding.authorization_generation != anchor.authorization_generation
                or binding.authorization_generation != grant.authorization_generation
                or binding.revision != grant.revision
                or binding.fence != grant.fence
                or binding.expires_at <= now
                or any(
                    prepared.get(key) != binding.model_dump(mode="json")[key]
                    for key in (
                        "identity",
                        "namespace",
                        "workspace_id",
                        "preparation_id",
                        "purpose",
                        "target",
                    )
                )
                or Preparation.model_validate(
                    {key: prepared[key] for key in Preparation.model_fields}
                ).expires_at
                < binding.expires_at
            ):
                raise MaterialDeniedError("RECOVERY_BINDINGS_REQUIRED")
            matches.append(binding)
        if len(matches) != 1:
            raise MaterialDeniedError("RECOVERY_BINDINGS_REQUIRED")
        selected[role] = matches[0]
    return HostedWorkspace.model_validate(
        {"workspace_id": old.workspace_id, "credentials": selected}
    )


@dataclass
class RetainedWorkPlan:
    acceptance: RecoveryAcceptance
    changes: list[tuple[Workspace, str, HostedWorkspace | None]]

    def bindings(self) -> list[Binding]:
        return [
            getattr(fresh.credentials, role)
            for _, _, fresh in self.changes
            if fresh is not None
            for role in ("agent", "github")
        ]

    def snapshot(self) -> list[tuple[str, int, str, HostedWorkspace | None]]:
        return [
            (workspace.id, workspace.version, disposition, fresh)
            for workspace, disposition, fresh in self.changes
        ]


async def inspect_retained_work(
    session: AsyncSession,
    gate: NodeLifecycleRow,
    value: RecoveryAcknowledgement,
) -> RetainedWorkPlan:
    identity = Identity.model_validate(gate.identity)
    previous = Identity.model_validate(gate.previous_identity) if gate.previous_identity else None
    counts: Counter[str] = Counter()
    changes: list[tuple[Workspace, str, HostedWorkspace | None]] = []
    if await session.scalar(
        select(Task.id)
        .where(~select(TaskAttempt.id).where(TaskAttempt.task_id == Task.id).exists())
        .limit(1)
    ):
        # Quiescence does not inventory logical tasks without any owned attempt.
        _unresolved()
    idle = await database_quiescent(session)
    if not idle:
        if previous is None:
            _unresolved()
        active_attempt = TaskAttempt.status.not_in(_TERMINAL)
        active_operation = Operation.status.not_in(_OP_TERMINAL)
        active_validation = ValidationRun.status.not_in(("succeeded", "failed"))
        active_reservation = ResourceReservation.released_at.is_(None)
        active_merge = MergeCandidate.status.not_in(("merged", "closed"))
        active_lease = WorkspaceSecretLease.status.not_in(("revoked", "released"))
        active_callback = CallbackDelivery.status.not_in(("succeeded", "failed", "skipped"))
        families = (
            (TaskAttempt, active_attempt),
            (Operation, active_operation),
            (ValidationRun, active_validation),
            (ResourceReservation, active_reservation),
            (MergeCandidate, active_merge),
            (WorkspaceSecretLease, active_lease),
            (CallbackDelivery, active_callback),
        )
        invalid = [
            select(ServiceGCRequest.id).where(
                ServiceGCRequest.status.not_in(("completed", "failed", "expired"))
            ),
            select(ResourceReservation.id).where(
                active_reservation, ResourceReservation.node_id != identity.owner
            ),
            select(CallbackDelivery.id).where(
                active_callback,
                or_(
                    CallbackDelivery.workspace_id.is_(None),
                    ~select(CallbackSubscription.id)
                    .where(CallbackSubscription.id == CallbackDelivery.subscription_id)
                    .exists(),
                ),
            ),
        ]
        for model, active in families:
            invalid.append(
                select(model.id).where(
                    active, ~select(Workspace.id).where(Workspace.id == model.workspace_id).exists()
                )
            )
        for model, active in (
            (ValidationRun, active_validation),
            (ResourceReservation, active_reservation),
            (MergeCandidate, active_merge),
            (WorkspaceSecretLease, active_lease),
        ):
            invalid.append(
                select(model.id).where(
                    active,
                    model.attempt_id.is_not(None),
                    ~select(TaskAttempt.id)
                    .where(
                        TaskAttempt.id == model.attempt_id,
                        TaskAttempt.workspace_id == model.workspace_id,
                    )
                    .exists(),
                )
            )
        invalid.append(
            select(MergeCandidate.id).where(
                active_merge,
                ~select(TaskAttempt.id)
                .where(
                    TaskAttempt.id == MergeCandidate.attempt_id,
                    TaskAttempt.task_id == MergeCandidate.task_id,
                )
                .exists(),
            )
        )
        for model, reference in (
            (Operation, CallbackDelivery.operation_id),
            (MergeCandidate, CallbackDelivery.merge_candidate_id),
        ):
            invalid.append(
                select(CallbackDelivery.id).where(
                    active_callback,
                    reference.is_not(None),
                    ~select(model.id)
                    .where(
                        model.id == reference, model.workspace_id == CallbackDelivery.workspace_id
                    )
                    .exists(),
                )
            )
        for query in invalid:
            if (await session.execute(query.limit(1))).first() is not None:
                _unresolved()

        # The lifecycle mutex already serializes all ordinary guarded writes.
        # Historical events and terminal rows are evidence, not recovery work.
        # Disable default selectin relationships as well as explicit hydration.
        actionable = or_(
            Workspace.status.not_in(_TERMINAL),
            Workspace.execution_claimed_by.is_not(None),
            Workspace.monitor_claimed_by.is_not(None),
            Workspace.task_policy[RECOVERY_POLICY_KEY]["disposition"].as_string() == "attention",
            and_(
                Workspace.compose_project_name.is_not(None),
                ~terminal_runtime_effectively_released_expr(correlated_to=Workspace),
            ),
            *(
                select(model.id).where(model.workspace_id == Workspace.id, active).exists()
                for model, active in families
            ),
        )
        workspaces = list(
            await session.scalars(
                select(Workspace)
                .where(actionable)
                .options(raiseload("*"))
                .order_by(Workspace.id)
                .limit(MAX_RETAINED_WORKSPACES + 1)
                .with_for_update()
            )
        )
        if len(workspaces) > MAX_RETAINED_WORKSPACES:
            _unresolved()
        for workspace in workspaces:
            prior = (workspace.task_policy or {}).get(RECOVERY_POLICY_KEY)
            old = HostedWorkspace.model_validate((workspace.task_policy or {}).get("hosted_node"))
            if workspace.node_id not in (None, identity.owner) or old.workspace_id != workspace.id:
                _unresolved()
            old_identity = old.credentials.agent.identity
            parked = bool(prior and prior.get("disposition") == "attention")
            if old_identity != previous and not (
                parked
                and isinstance(prior, dict)
                and Identity.model_validate(prior["identity"]).owner == identity.owner
                and Identity.model_validate(prior["identity"]).boot_epoch <= previous.boot_epoch
                and prior.get("previous_identity") == old_identity.model_dump()
                and old_identity.owner == identity.owner
                and previous is not None
                and old_identity.boot_epoch < previous.boot_epoch
            ):
                _unresolved()
            attempt = TaskAttempt.workspace_id == workspace.id
            (
                has_attempt,
                bad_queue_attempt,
                bad_monitor_attempt,
                has_ops,
                has_runs,
                pending_run,
                pending_callback,
                reserved,
                has_merge,
                bad_merge,
            ) = (
                await session.execute(
                    select(
                        select(TaskAttempt.id).where(attempt).exists(),
                        select(TaskAttempt.id)
                        .where(
                            attempt,
                            or_(
                                TaskAttempt.status != "requested",
                                TaskAttempt.attempt_number != 1,
                                TaskAttempt.parent_attempt_id.is_not(None),
                                TaskAttempt.redispatch_from_attempt_id.is_not(None),
                            ),
                        )
                        .exists(),
                        select(TaskAttempt.id)
                        .where(attempt, TaskAttempt.status.not_in({"monitoring_pr", *_TERMINAL}))
                        .exists(),
                        select(Operation.id).where(Operation.workspace_id == workspace.id).exists(),
                        select(ValidationRun.id)
                        .where(ValidationRun.workspace_id == workspace.id)
                        .exists(),
                        select(ValidationRun.id)
                        .where(ValidationRun.workspace_id == workspace.id, active_validation)
                        .exists(),
                        select(CallbackDelivery.id)
                        .where(CallbackDelivery.workspace_id == workspace.id, active_callback)
                        .exists(),
                        select(ResourceReservation.id)
                        .where(ResourceReservation.workspace_id == workspace.id, active_reservation)
                        .exists(),
                        select(MergeCandidate.id)
                        .where(MergeCandidate.workspace_id == workspace.id, active_merge)
                        .exists(),
                        select(MergeCandidate.id)
                        .where(
                            MergeCandidate.workspace_id == workspace.id,
                            active_merge,
                            or_(
                                MergeCandidate.status != "open",
                                MergeCandidate.pr_url.is_distinct_from(workspace.pr_url),
                                MergeCandidate.pr_number.is_distinct_from(workspace.pr_number),
                            ),
                        )
                        .exists(),
                    )
                )
            ).one()
            if not has_attempt:
                _unresolved()
            ops = list(
                await session.scalars(
                    select(Operation)
                    .where(Operation.workspace_id == workspace.id, active_operation)
                    .options(raiseload("*"))
                    .limit(MAX_ACTIVE_OPERATIONS_PER_WORKSPACE + 1)
                )
            )
            if len(ops) > MAX_ACTIVE_OPERATIONS_PER_WORKSPACE:
                _unresolved()
            unresolved = any(
                op.status not in _OP_TERMINAL and not _passive_monitor_operation(op, workspace)
                for op in ops
            )
            unresolved |= pending_run or pending_callback or bad_merge
            queued = (
                workspace.status == "requested"
                and not parked
                and not has_ops
                and not has_runs
                and not pending_callback
                and not has_merge
                and (
                    workspace.execution_claim_epoch == 0
                    or (
                        isinstance(prior, dict)
                        and prior.get("disposition") == "queued"
                        and prior.get("identity") == gate.previous_identity
                        and prior.get("execution_claim_epoch") == workspace.execution_claim_epoch
                        and prior.get("workspace_version") == workspace.version
                    )
                )
                and workspace.execution_claimed_by is None
                and workspace.execution_claim_expires_at is None
                and workspace.monitor_claimed_by is None
                and workspace.monitor_claim_expires_at is None
                and workspace.compose_project_name is None
                and workspace.compose_file_path is None
                and workspace.pr_url is None
                and workspace.monitor_started_at is None
                and not bad_queue_attempt
            )
            monitor = (
                workspace.status == "monitoring_pr"
                and bool(workspace.pr_url)
                and workspace.pr_number is not None
                and workspace.pr_number > 0
                and not parked
                and not unresolved
                and not bad_monitor_attempt
            )
            disposition = "queued" if queued else "monitor" if monitor else "attention"
            if workspace.node_id is None and not (
                reserved and (queued or disposition == "attention")
            ):
                # HTTP admission reserves this node before provisioning stamps
                # placement. A reserved unknown outcome may only be parked.
                _unresolved()
            fresh = (
                await _fresh_association(session, old, identity)
                if disposition != "attention"
                else None
            )
            changes.append((workspace, disposition, fresh))
            counts[disposition] += 1

    acceptance = RecoveryAcceptance(
        **value.model_dump(),
        previous_identity=previous,
        disk_ref=gate.disk_ref,
        database_ref=gate.database_ref,
        queued_workspaces=counts["queued"],
        monitor_workspaces=counts["monitor"],
        attention_workspaces=counts["attention"],
    )
    return RetainedWorkPlan(acceptance, changes)


async def reconcile(
    session: AsyncSession,
    gate: NodeLifecycleRow,
    value: RecoveryAcknowledgement,
    plan: RetainedWorkPlan,
) -> RecoveryAcceptance:
    identity = Identity.model_validate(gate.identity)
    acceptance, changes = plan.acceptance, plan.changes
    # This is the final transition, never a temporary bypass. Other guarded
    # writers cannot observe ready until every reconciliation mutation commits.
    gate.phase = "ready"
    gate.recovery_key, gate.receipt_ref = value.idempotency_key, value.receipt_ref
    gate.recovery_acceptance = acceptance.model_dump(mode="json")
    await session.flush()
    # Revoke active grants by their immutable preparation identity, not by
    # hydrating every historical renewal/install receipt for each grant.
    if await session.scalar(
        select(Grant.id)
        .where(
            Grant.revoked.is_(False),
            ~select(PreparationRow.id).where(PreparationRow.id == Grant.preparation).exists(),
        )
        .limit(1)
    ):
        _unresolved()
    preparations = (
        await session.execute(
            select(PreparationRow.id, PreparationRow.binding)
            .where(
                select(Grant.id)
                .where(Grant.preparation == PreparationRow.id, Grant.revoked.is_(False))
                .exists()
            )
            .limit(MAX_ACTIVE_PREPARATIONS + 1)
        )
    ).all()
    if len(preparations) > MAX_ACTIVE_PREPARATIONS:
        _unresolved()
    retired = []
    for preparation_id, binding in preparations:
        bound = Identity.model_validate(binding["identity"])
        if bound != identity:
            if bound.owner != identity.owner or bound.boot_epoch >= identity.boot_epoch:
                _unresolved()
            retired.append(preparation_id)
    if retired:
        await session.execute(
            update(Grant)
            .where(Grant.preparation.in_(retired), Grant.revoked.is_(False))
            .values(revoked=True, current=None, pending=None)
        )
    for workspace, disposition, fresh in changes:
        prior = (workspace.task_policy or {}).get(RECOVERY_POLICY_KEY)
        if prior and prior.get("disposition") == "attention":
            # Its immutable fence already contains the unresolved outcome. A
            # later boot counts it without rewriting evidence or reopening it.
            continue
        # Revoke retained legacy leases before marking attention immutable.
        await session.execute(
            update(WorkspaceSecretLease)
            .where(
                WorkspaceSecretLease.workspace_id == workspace.id,
                WorkspaceSecretLease.status.not_in(("revoked", "released")),
            )
            .values(
                status="revoked",
                revoked_at=datetime.now(UTC),
                revoke_reason_code="HOSTED_RECOVERY_REVOKED",
            )
        )
        await session.flush()
        workspace.execution_claimed_by = workspace.execution_claim_expires_at = None
        workspace.node_id = identity.owner
        workspace.monitor_claimed_by = workspace.monitor_claim_expires_at = None
        workspace.execution_claim_epoch += 1
        workspace.version += 1
        workspace.event_sequence += 1
        # Event first so an existing attention row's guard needs no mutation
        # escape hatch. Subsequent boots retain the original parked disposition.
        session.add(
            WorkspaceEvent(
                id=str(uuid4()),
                workspace_id=workspace.id,
                event_type="workspace.recovery_accepted",
                old_state=workspace.status,
                new_state=workspace.status,
                reason_code="RECOVERY_ATTENTION_REQUIRED"
                if disposition == "attention"
                else "RECOVERY_ACCEPTED",
                event_order=workspace.event_sequence,
                payload={
                    **value.model_dump(),
                    "identity": gate.identity,
                    "disposition": disposition,
                },
            )
        )
        await session.flush()
        policy = {
            **workspace.task_policy,
            RECOVERY_POLICY_KEY: {
                **value.model_dump(),
                "identity": gate.identity,
                "previous_identity": gate.previous_identity,
                "disposition": disposition,
                "reason_code": "RECOVERY_ATTENTION_REQUIRED"
                if disposition == "attention"
                else "RECOVERY_ACCEPTED",
                "workspace_version": workspace.version,
                "execution_claim_epoch": workspace.execution_claim_epoch,
            },
        }
        if fresh is not None:
            policy["hosted_node"] = fresh.model_dump(mode="json")
        workspace.task_policy = policy
        await session.flush()
    return acceptance


async def accept_retained_work(
    session: AsyncSession,
    gate: NodeLifecycleRow,
    value: RecoveryAcknowledgement,
    expected: RetainedWorkPlan,
) -> RecoveryAcceptance:
    try:
        plan = await inspect_retained_work(session, gate, value)
        if plan.acceptance != expected.acceptance or plan.snapshot() != expected.snapshot():
            raise MaterialDeniedError("RECOVERY_BINDINGS_REQUIRED")
        return await reconcile(session, gate, value, plan)
    except (ValidationError, KeyError, TypeError):
        raise MaterialDeniedError("RECOVERY_OUTCOME_UNRESOLVED") from None
