"""Historical ownership and atomic fresh material association on ordinary controls."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.models import Operation, Workspace
from awf.hosted_node.identity import MaterialDeniedError, check
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_operations import check_source, lock_materials, require_source_quiet
from awf.hosted_node.models import Grant, PreparationRow, Receipt
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_schemas import (
    POLICY_KEY,
    HostedControlAuthorization,
    HostedReauthorization,
    HostedWorkspace,
)
from awf.hosted_node.schemas import Identity


async def historical_source(
    session: AsyncSession, authority: WorkspaceAuthority, row: Workspace | None, namespace: str
) -> HostedWorkspace:
    association = check_source(authority, row, namespace, historical=True)
    if association.credentials.agent.identity == authority.receiver.identity:
        return association
    assert row is not None
    await require_source_quiet(session, row)
    await historical_evidence(session, authority, association)
    return association


async def historical_evidence(
    session: AsyncSession, authority: WorkspaceAuthority, association: HostedWorkspace
) -> None:
    receiver = authority.receiver
    owner = await check(session, receiver.identity)
    gate = await session.get(NodeLifecycleRow, 1, populate_existing=True)
    storage = receiver.config.lifecycle
    if (
        owner.acknowledgement is None
        or gate is None
        or gate.identity != receiver.identity.model_dump()
        or gate.phase != "ready"
        or gate.recovery_key is None
        or gate.restore_quarantine is not None
        or storage is None
        or gate.disk_ref != storage.disk_ref
        or gate.database_ref != storage.database_ref
    ):
        raise MaterialDeniedError("NODE_ADMISSION_CLOSED", 409)
    if association.credentials.agent.identity != receiver.identity:
        accepted = gate.recovery_acceptance or {}
        if any(
            accepted.get(key) != value
            for key, value in {
                "idempotency_key": gate.recovery_key,
                "receipt_ref": gate.receipt_ref,
                "disk_ref": gate.disk_ref,
                "database_ref": gate.database_ref,
                "previous_identity": gate.previous_identity,
            }.items()
        ):
            raise MaterialDeniedError("HOSTED_WORKSPACE_HISTORY_MISMATCH", 403)
        await require_nonrewound_history(session, gate, association.credentials.agent.identity)
    # Exact persisted installation proves historical association, not current
    # material usability. Expiry/revocation cannot erase ownership or revive a
    # grant. reset_boot forbids unapproved storage changes; boot monotonically
    # fences generations/epochs and preserves the last accepted predecessor.
    # Every accepted replacement has therefore crossed the physical proof gate.
    for binding in (association.credentials.agent, association.credentials.github):
        evidence = (
            await session.execute(
                select(PreparationRow, Grant, Receipt)
                .join(Grant, Grant.preparation == PreparationRow.id)
                .join(Receipt, Receipt.grant == Grant.id)
                .where(
                    Receipt.owner == receiver.identity.owner,
                    Receipt.idempotency_key == binding.idempotency_key,
                )
            )
        ).one_or_none()
        if evidence is None:
            raise MaterialDeniedError("HOSTED_WORKSPACE_HISTORY_MISMATCH", 403)
        prep, grant, receipt = evidence
        if (
            prep.owner != receiver.identity.owner
            or prep.namespace != binding.namespace
            or prep.workspace_id != association.workspace_id
            or prep.preparation_id != binding.preparation_id
            or prep.binding["identity"] != binding.identity.model_dump()
            or prep.binding["target"] != binding.target
            or prep.binding["purpose"] != binding.purpose
            or grant.grant_id != binding.grant_id
            or receipt.binding != binding.model_dump(mode="json")
            or receipt.operation not in {"install", "refresh"}
            or receipt.state not in {"installed", "expired"}
        ):
            raise MaterialDeniedError("HOSTED_WORKSPACE_HISTORY_MISMATCH", 403)


async def require_nonrewound_history(
    session: AsyncSession, gate: NodeLifecycleRow, source: Identity
) -> None:
    if gate.restore_reconciliation_id is None:
        return
    journal = await session.get(RestoreReconciliationRow, gate.restore_reconciliation_id)
    if journal is None or journal.state != "committed" or journal.owner != source.owner:
        raise MaterialDeniedError("HOSTED_WORKSPACE_HISTORY_MISMATCH", 403)
    restored = Identity.model_validate(journal.identity)
    # Rewound terminal rows were not classified as safe replay. Only an actual
    # post-restore association may use subsequent ordinary replacement history.
    if (
        source.owner != restored.owner
        or source.boot_epoch < restored.boot_epoch
        or source.recovery_generation < restored.recovery_generation
        or (
            source.recovery_generation == restored.recovery_generation
            and source.incarnation != restored.incarnation
        )
    ):
        raise MaterialDeniedError("RECOVERY_ATTENTION_REQUIRED", 409)


async def authorize_rebinding(
    session: AsyncSession,
    authority: WorkspaceAuthority,
    workspace_id: str,
    authorization: HostedControlAuthorization,
    pair: HostedReauthorization,
) -> None:
    row = await session.get(Workspace, workspace_id)
    association = check_source(authority, row, authorization.namespace, historical=True)
    if association not in (pair.expected_association, pair.replacement_association):
        raise MaterialDeniedError("HOSTED_REAUTHORIZATION_PREIMAGE_MISMATCH", 409)
    assert row is not None
    await authority.materials(pair.replacement_association, row.agent, current=True)
    await lock_materials(session, authority, pair.replacement_association)
    await session.refresh(row, with_for_update=True)
    association = check_source(authority, row, authorization.namespace, historical=True)
    if association not in (pair.expected_association, pair.replacement_association):
        raise MaterialDeniedError("HOSTED_REAUTHORIZATION_PREIMAGE_MISMATCH", 409)
    if association == pair.expected_association:
        await require_rebind_quiet(session, authority, row, association)
        await historical_evidence(session, authority, association)


async def require_rebind_quiet(
    session: AsyncSession,
    authority: WorkspaceAuthority,
    row: Workspace,
    association: HostedWorkspace,
) -> None:
    if (
        row.execution_claimed_by is not None
        or row.monitor_claimed_by is not None
        or await session.scalar(
            select(Operation.id)
            .where(Operation.workspace_id == row.id, Operation.status.in_(("pending", "running")))
            .limit(1)
        )
        is not None
    ):
        raise MaterialDeniedError("HOSTED_SOURCE_NOT_QUIESCENT", 409)
    if row.status in {"blocked", "monitoring_pr"}:
        # Paused work may retain its warm stack. The node/workspace locks exclude
        # a new claim through commit; no old invocation or uncertain operation
        # may cross this fresh-authority boundary. Old-boot nonterminal work must
        # first pass the existing retained-work reconciliation.
        if association.credentials.agent.identity != authority.receiver.identity:
            raise MaterialDeniedError("HOSTED_WORKSPACE_BINDING_MISMATCH", 403)
    else:
        await require_source_quiet(session, row)


def apply_reauthorization(row: Workspace, pair: HostedReauthorization | None) -> None:
    if pair is None:
        return
    if row.task_policy.get(POLICY_KEY) != pair.expected_association.model_dump(mode="json"):
        # A stored replacement is allowed only for the exact operation replay,
        # which returns before this point. It is not a wildcard CAS preimage.
        raise MaterialDeniedError("HOSTED_REAUTHORIZATION_PREIMAGE_MISMATCH", 409)
    row.task_policy = {
        **row.task_policy,
        POLICY_KEY: pair.replacement_association.model_dump(mode="json"),
    }


def control_identity(
    authorization: HostedControlAuthorization | None, pair: HostedReauthorization | None
) -> dict[str, object]:
    return {
        **({"hosted_authorization": authorization.model_dump()} if authorization else {}),
        **({"hosted_reauthorization": pair.model_dump(mode="json")} if pair else {}),
    }


def remonitor_payload(
    workspace: Workspace,
    reason: str | None,
    authorization: HostedControlAuthorization | None,
    pair: HostedReauthorization | None,
    expected_version: int | None,
) -> tuple[dict[str, object | None], dict[str, object | None] | None]:
    from awf.service.controls_helpers import (
        _operation_payload,
        _operator_operation_payload,
        _workspace_pr_operation_context,
    )

    base = _operator_operation_payload(
        reason=reason,
        reason_code="OPERATOR_REMONITOR",
        requested_action="remonitor",
        extra=control_identity(authorization, pair),
    )
    # PR/head provenance is recorded once, not a mutable command identity. The
    # existing ordinary (non-reauthorized) control comparison stays unchanged.
    return (
        _operation_payload(
            {**base, **_workspace_pr_operation_context(workspace)},
            expected_version=expected_version,
        ),
        _operation_payload(base, expected_version=expected_version) if pair else None,
    )
