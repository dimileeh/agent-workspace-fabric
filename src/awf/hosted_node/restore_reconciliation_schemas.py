"""Nonsecret server-only restore decisions; never accepted as lifecycle HTTP input."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from awf.hosted_node.checkpoint_schemas import Digest
from awf.hosted_node.restore_schemas import RestoreBinding
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Binding, Generation, Identity, Opaque, StrictModel

MAX_RESTORE_ROWS = 16384
MAX_RESTORE_WORKSPACES = 1024
RowId = Annotated[str, Field(min_length=1, max_length=64)]
Revision = Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
Family = Literal[
    "workspaces",
    "tasks",
    "task_attempts",
    "operations",
    "validation_runs",
    "resource_reservations",
    "service_gc_requests",
    "callback_deliveries",
    "callback_subscriptions",
    "merge_candidates",
    "workspace_events",
    "workspace_secret_leases",
]
OrphanFamily = Literal[
    "tasks", "callback_subscriptions", "callback_deliveries", "service_gc_requests"
]


def canonical_sha256(value: Any) -> str:
    """UTF-8 JSON, sorted keys, compact separators, ASCII escapes, no NaN/Infinity."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


class RestoreInventoryRow(StrictModel):
    family: Family
    row_id: RowId
    preimage_sha256: Digest
    workspace_id: RowId | None = None
    task_id: RowId | None = None
    attempt_id: RowId | None = None
    subscription_id: RowId | None = None
    operation_id: RowId | None = None
    merge_candidate_id: RowId | None = None
    status: Annotated[str, Field(max_length=32)] | None = None
    operation_type: Annotated[str, Field(max_length=32)] | None = None
    node_id: Annotated[str, Field(max_length=128)] | None = None
    external_id: Annotated[str, Field(max_length=128)] | None = None
    request_key: Annotated[str, Field(max_length=256)] | None = None
    source_id: Annotated[str, Field(max_length=64)] | None = None
    repo_url: Annotated[str, Field(max_length=512)] | None = None
    branch_base: Annotated[str, Field(max_length=256)] | None = None
    pr_url: Annotated[str, Field(max_length=512)] | None = None
    pr_number: Annotated[int, Field(strict=True, ge=1)] | None = None
    head_sha: Annotated[str, Field(max_length=64)] | None = None
    hosted: HostedWorkspace | None = None
    target_url: Annotated[str, Field(max_length=2048)] | None = None
    event_types: Annotated[
        tuple[Annotated[str, Field(max_length=64)], ...], Field(max_length=128)
    ] = ()
    parameters_sha256: Digest | None = None
    history_count: Annotated[int, Field(strict=True, ge=0)] | None = None


class RestoreInventory(StrictModel):
    version: Literal[1] = 1
    preimage_format: Literal["postgres-jsonb-row-sha256-v1"] = "postgres-jsonb-row-sha256-v1"
    binding: RestoreBinding
    identity: Identity
    idempotency_key: Opaque
    receipt_ref: Opaque
    prior_revision: Revision
    initial: bool
    rows: Annotated[tuple[RestoreInventoryRow, ...], Field(max_length=MAX_RESTORE_ROWS)]
    inventory_sha256: Digest

    @model_validator(mode="after")
    def canonical(self) -> "RestoreInventory":
        keys = [(row.family, row.row_id) for row in self.rows]
        if keys != sorted(set(keys)) or self.inventory_sha256 != canonical_sha256(
            self.model_dump(mode="json", exclude={"inventory_sha256"})
        ):
            raise ValueError("RESTORE_INVENTORY_DIGEST_MISMATCH")
        return self


def action_set_sha256(rows: tuple[RestoreInventoryRow, ...], workspace_id: str) -> str:
    """Bind the exact restored action/relationship set, never its apparent emptiness."""
    return canonical_sha256(
        {
            "workspace_id": workspace_id,
            "rows": [
                {"family": row.family, "row_id": row.row_id, "preimage_sha256": row.preimage_sha256}
                for row in rows
                if row.workspace_id == workspace_id and row.family != "workspace_events"
            ],
        }
    )


class RestoreActionEvidence(StrictModel):
    action_id: Annotated[str, Field(min_length=1, max_length=128)]
    outcome: Literal[
        "unknown",
        "never_started",
        "monitor_safe",
        "succeeded",
        "failed",
        "current_routing",
        "safe_gc",
    ]
    evidence_ref: Opaque
    evidence_sha256: Digest
    observed_at: AwareDatetime
    external_action_ref: Annotated[str, Field(min_length=1, max_length=256)] | None = None

    @model_validator(mode="after")
    def positive_requires_action(self) -> "RestoreActionEvidence":
        if self.outcome != "unknown" and self.external_action_ref is None:
            raise ValueError("RESTORE_KNOWN_ACTION_REQUIRED")
        return self


class RestoreOperationDecision(StrictModel):
    operation_id: RowId
    preimage_sha256: Digest
    fact: RestoreActionEvidence

    @model_validator(mode="after")
    def operation_fact(self) -> "RestoreOperationDecision":
        if self.fact.action_id != self.operation_id or self.fact.outcome not in (
            "unknown",
            "never_started",
            "succeeded",
            "failed",
        ):
            raise ValueError("RESTORE_OPERATION_FACT_MISMATCH")
        return self


class RestoreWorkspaceDecision(StrictModel):
    workspace_id: RowId
    preimage_sha256: Digest
    action_set_sha256: Digest
    disposition: Literal["held", "queued", "monitor"]
    fact: RestoreActionEvidence
    operations: Annotated[tuple[RestoreOperationDecision, ...], Field(max_length=256)] = ()
    fresh_association: HostedWorkspace | None = None

    @model_validator(mode="after")
    def exact_continuation(self) -> "RestoreWorkspaceDecision":
        expected = {"held": "unknown", "queued": "never_started", "monitor": "monitor_safe"}
        if (
            self.fact.action_id != self.workspace_id
            or self.fact.outcome != expected[self.disposition]
        ):
            raise ValueError("RESTORE_WORKSPACE_FACT_MISMATCH")
        if self.disposition == "held":
            if self.fresh_association is not None or self.operations:
                raise ValueError("RESTORE_HELD_MUTATION_FORBIDDEN")
        elif (
            self.fresh_association is None
            or self.fresh_association.workspace_id != self.workspace_id
        ):
            raise ValueError("RESTORE_FRESH_ASSOCIATION_REQUIRED")
        ids = [item.operation_id for item in self.operations]
        if ids != sorted(set(ids)):
            raise ValueError("RESTORE_OPERATION_SET_INVALID")
        return self


class RestoreRowDecision(StrictModel):
    family: OrphanFamily
    row_id: RowId
    preimage_sha256: Digest
    disposition: Literal["held", "resume"]
    fact: RestoreActionEvidence

    @model_validator(mode="after")
    def exact_row(self) -> "RestoreRowDecision":
        if self.family != "service_gc_requests" and len(self.row_id) > 36:
            raise ValueError("RESTORE_ROW_ID_INVALID")
        expected = {
            "tasks": "never_started",
            "callback_subscriptions": "current_routing",
            "callback_deliveries": "never_started",
            "service_gc_requests": "safe_gc",
        }
        if self.fact.action_id != self.row_id or self.fact.outcome != (
            "unknown" if self.disposition == "held" else expected[self.family]
        ):
            raise ValueError("RESTORE_ROW_FACT_MISMATCH")
        return self


class RestoreReconciliationEvidence(StrictModel):
    binding: RestoreBinding
    identity: Identity
    idempotency_key: Opaque
    receipt_ref: Opaque
    prior_revision: Revision
    inventory_sha256: Digest
    authority_revision: Generation
    authorization_sha256: Digest
    external_facts_sha256: Digest
    post_checkpoint_frontier_sha256: Digest
    workspaces: Annotated[
        tuple[RestoreWorkspaceDecision, ...], Field(max_length=MAX_RESTORE_WORKSPACES)
    ]
    rows: Annotated[tuple[RestoreRowDecision, ...], Field(max_length=MAX_RESTORE_ROWS)]
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def ordered_decisions(self) -> "RestoreReconciliationEvidence":
        workspace_ids = [row.workspace_id for row in self.workspaces]
        row_ids = [(row.family, row.row_id) for row in self.rows]
        if workspace_ids != sorted(set(workspace_ids)) or row_ids != sorted(set(row_ids)):
            raise ValueError("RESTORE_DECISION_SET_INVALID")
        return self


def reconciliation_plan_sha256(value: RestoreReconciliationEvidence) -> str:
    return canonical_sha256(value.model_dump(mode="json", exclude={"expires_at"}))


class RestoreMaterialRequest(StrictModel):
    journal_id: Opaque
    restore_binding: RestoreBinding
    identity: Identity
    idempotency_key: Opaque
    receipt_ref: Opaque
    prior_revision: Revision
    inventory_sha256: Digest
    plan_sha256: Digest
    authority_revision: Generation
    authorization_sha256: Digest
    operation: Literal["prepare", "install"]
    proposed: Binding


class RestoreMaterialPermit(StrictModel):
    request: RestoreMaterialRequest
    expires_at: AwareDatetime


VerifyRestoreReconciliation = Callable[
    [RestoreInventory], Awaitable[RestoreReconciliationEvidence | None]
]
VerifyRestoreMaterial = Callable[[RestoreMaterialRequest], Awaitable[RestoreMaterialPermit | None]]
