"""Bounded actionable SQL inventory; historical events stay SQL aggregates."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.restore_quarantine import NONTERMINAL
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.restore_reconciliation_schemas import (
    MAX_RESTORE_ROWS,
    MAX_RESTORE_WORKSPACES,
    RestoreInventory,
    RestoreInventoryRow,
    canonical_sha256,
)
from awf.hosted_node.restore_schemas import RestoreBinding
from awf.hosted_node.schemas import Identity

ROW_DIGEST = "encode(sha256(convert_to(to_jsonb(t)::text, 'UTF8')), 'hex')"
TERMINAL = "('completed','failed','cancelled','destroyed')"


async def context(session: AsyncSession, gate: NodeLifecycleRow) -> tuple[RestoreBinding, int]:
    if gate.restore_quarantine is not None:
        if gate.restore_quarantine.get("state") != "held":
            raise MaterialDeniedError("RESTORE_STATE_CONFLICT")
        return RestoreBinding.model_validate(gate.restore_quarantine["binding"]), 0
    journal = (
        await session.get(RestoreReconciliationRow, gate.restore_reconciliation_id)
        if gate.restore_reconciliation_id
        else None
    )
    if journal is None or journal.state != "committed" or journal.owner != gate.identity["owner"]:
        raise MaterialDeniedError("RESTORE_STATE_CONFLICT")
    return RestoreBinding.model_validate(journal.binding), journal.revision


async def inventory(
    session: AsyncSession, gate: NodeLifecycleRow, value: RecoveryAcknowledgement
) -> tuple[RestoreInventory, dict[tuple[str, str], dict[str, Any]]]:
    binding, revision = await context(session, gate)
    initial = gate.restore_quarantine is not None
    records: dict[tuple[str, str], dict[str, Any]] = {}
    digests: dict[tuple[str, str], str] = {}

    async def read(family: str, predicate: str, params: dict[str, Any] | None = None) -> None:
        # Family/predicate originate solely in the fixed Core model mapping below.
        rows = (
            await session.execute(
                text(
                    f"SELECT to_jsonb(t) AS value, {ROW_DIGEST} AS digest FROM {family} t "
                    f"WHERE {predicate} ORDER BY t.id LIMIT :cap"
                ),
                {**(params or {}), "cap": MAX_RESTORE_ROWS + 1},
            )
        ).all()
        for row, digest in rows:
            key = family, row["id"]
            records[key], digests[key] = row, digest
        if len(records) > MAX_RESTORE_ROWS:
            raise MaterialDeniedError("RESTORE_INVENTORY_UNSUPPORTED")

    if initial:
        workspace_predicate = (
            f"status NOT IN {TERMINAL} OR execution_claimed_by IS NOT NULL OR monitor_claimed_by IS NOT NULL "
            "OR task_policy::jsonb #>> '{hosted_recovery,disposition}' = 'attention'"
        )
    else:
        workspace_predicate = (
            "task_policy::jsonb #>> '{hosted_recovery,disposition}' = 'attention' "
            "AND task_policy::jsonb #>> '{hosted_recovery,restore_id}' = :restore_id"
        )
    await read("workspaces", workspace_predicate, {"restore_id": binding.restore_id})
    workspace_ids = [key[1] for key in records if key[0] == "workspaces"]
    for family in NONTERMINAL:
        if family in {"workspaces", "tasks"}:
            continue
        if initial:
            predicate = NONTERMINAL[family]
        elif family in {"callback_deliveries", "service_gc_requests"}:
            predicate = (
                "EXISTS(SELECT 1 FROM hosted_restore_row_holds h WHERE h.owner=:owner "
                "AND h.restore_id=:restore_id AND h.family=:family AND h.row_id=t.id AND h.state='held')"
            )
            if family == "callback_deliveries":
                predicate += (
                    " OR workspace_id = ANY(:workspaces) "
                    "OR EXISTS(SELECT 1 FROM operations o WHERE o.id=t.operation_id AND o.workspace_id=ANY(:workspaces)) "
                    "OR EXISTS(SELECT 1 FROM merge_candidates m WHERE m.id=t.merge_candidate_id AND m.workspace_id=ANY(:workspaces)) "
                    "OR EXISTS(SELECT 1 FROM workspace_events e WHERE e.id=t.source_id AND e.workspace_id=ANY(:workspaces))"
                )
        else:
            predicate = "workspace_id = ANY(:workspaces)"
        if not initial:
            predicate = f"({predicate}) AND ({NONTERMINAL[family]})"
        await read(
            family,
            predicate,
            {
                "owner": gate.identity["owner"],
                "restore_id": binding.restore_id,
                "family": family,
                "workspaces": workspace_ids,
            },
        )

    # Include exact parents even when the parent is terminal but a child is live.
    for family, field in (
        ("operations", "operation_id"),
        ("merge_candidates", "merge_candidate_id"),
        ("workspace_events", "source_id"),
    ):
        ids = [
            row[field]
            for (kind, _), row in records.items()
            if kind == "callback_deliveries" and row.get(field)
        ]
        if ids:
            await read(family, "id = ANY(:ids)", {"ids": ids})
    for (family, _), row in list(records.items()):
        if family == "callback_deliveries":
            parents = [row.get("workspace_id")]
            for parent, field in (
                ("operations", "operation_id"),
                ("merge_candidates", "merge_candidate_id"),
                ("workspace_events", "source_id"),
            ):
                if row.get(field) and (parent, row[field]) in records:
                    parents.append(records[parent, row[field]]["workspace_id"])
            parents = [identifier for identifier in parents if identifier]
            if len(set(parents)) > 1:
                raise MaterialDeniedError("RESTORE_ASSOCIATION_INVALID")
            row["_workspace"] = parents[0] if parents else None
    related = sorted(
        {
            str(row.get("_workspace", row.get("workspace_id")))
            for row in records.values()
            if row.get("_workspace", row.get("workspace_id"))
        }
    )
    if related:
        await read("workspaces", "id = ANY(:ids)", {"ids": related})
    workspace_ids = [key[1] for key in records if key[0] == "workspaces"]
    if len(workspace_ids) > MAX_RESTORE_WORKSPACES:
        raise MaterialDeniedError("RESTORE_INVENTORY_UNSUPPORTED")
    await read("task_attempts", "workspace_id = ANY(:ids)", {"ids": workspace_ids})
    task_ids = [row["task_id"] for (kind, _), row in records.items() if kind == "task_attempts"]
    task_predicate = "id = ANY(:ids)"
    if initial:
        task_predicate += " OR NOT EXISTS(SELECT 1 FROM task_attempts a WHERE a.task_id=t.id)"
    else:
        task_predicate += (
            " OR EXISTS(SELECT 1 FROM hosted_restore_row_holds h WHERE h.owner=:owner "
            "AND h.restore_id=:restore_id AND h.family='tasks' AND h.row_id=t.id AND h.state='held')"
        )
    await read(
        "tasks",
        task_predicate,
        {
            "ids": task_ids,
            "owner": gate.identity["owner"],
            "restore_id": binding.restore_id,
        },
    )
    subscriptions = [
        row["subscription_id"]
        for (kind, _), row in records.items()
        if kind == "callback_deliveries"
    ]
    subscription_predicate = "id = ANY(:ids)"
    if initial:
        subscription_predicate += " OR enabled"
    else:
        subscription_predicate += (
            " OR EXISTS(SELECT 1 FROM hosted_restore_row_holds h WHERE h.owner=:owner "
            "AND h.restore_id=:restore_id AND h.family='callback_subscriptions' AND h.row_id=t.id AND h.state='held')"
        )
    await read(
        "callback_subscriptions",
        subscription_predicate,
        {
            "ids": subscriptions,
            "owner": gate.identity["owner"],
            "restore_id": binding.restore_id,
        },
    )
    projected = []
    for (family, identifier), row in sorted(records.items()):
        workspace_id = (
            identifier if family == "workspaces" else row.get("_workspace", row.get("workspace_id"))
        )
        if row.get("node_id") not in (None, gate.identity["owner"]):
            raise MaterialDeniedError("RESTORE_OWNER_MISMATCH")
        if workspace_id is not None and ("workspaces", workspace_id) not in records:
            raise MaterialDeniedError("RESTORE_ASSOCIATION_INVALID")
        if row.get("attempt_id"):
            attempt = records.get(("task_attempts", row["attempt_id"]))
            if (
                attempt is None
                or attempt["workspace_id"] != workspace_id
                or (row.get("task_id") and row["task_id"] != attempt["task_id"])
            ):
                raise MaterialDeniedError("RESTORE_ASSOCIATION_INVALID")
        task_id = row.get("task_id")
        if family == "tasks":
            matches = {
                a["workspace_id"]
                for (kind, _), a in records.items()
                if kind == "task_attempts" and a["task_id"] == identifier
            }
            workspace_id = next(iter(matches)) if len(matches) == 1 else None
        projected.append(
            RestoreInventoryRow(
                family=family,
                row_id=identifier,
                preimage_sha256=digests[family, identifier],
                workspace_id=workspace_id,
                task_id=task_id,
                attempt_id=row.get("attempt_id"),
                subscription_id=row.get("subscription_id"),
                operation_id=row.get("operation_id"),
                merge_candidate_id=row.get("merge_candidate_id"),
                status=row.get("status"),
                operation_type=row.get("type"),
                node_id=row.get("node_id"),
                external_id=row.get("external_id", row.get("task_external_id")),
                request_key=row.get("idempotency_key"),
                source_id=row.get("source_id"),
                repo_url=row.get("repo_url"),
                branch_base=row.get("branch_base", row.get("base_branch")),
                pr_url=row.get("pr_url"),
                pr_number=row.get("pr_number"),
                head_sha=row.get("head_sha"),
                hosted=(row.get("task_policy") or {}).get("hosted_node")
                if family == "workspaces"
                else None,
                target_url=row.get("target_url"),
                event_types=tuple(row.get("event_types", ())),
                parameters_sha256=canonical_sha256(row["params"])
                if family == "service_gc_requests"
                else None,
            )
        )
    # Never hydrate an ever-growing event history. Its exact SQL aggregate pins
    # the stream while individual appended reconciliation events remain planned.
    if workspace_ids:
        histories = (
            await session.execute(
                text(
                    "SELECT workspace_id,count(*),encode(sha256(convert_to(string_agg("
                    "encode(sha256(convert_to(to_jsonb(e)::text,'UTF8')),'hex'),'' ORDER BY id),'UTF8')),'hex') "
                    "FROM workspace_events e WHERE workspace_id = ANY(:ids) GROUP BY workspace_id"
                ),
                {"ids": workspace_ids},
            )
        ).all()
        projected.extend(
            RestoreInventoryRow(
                family="workspace_events",
                row_id=workspace_id,
                workspace_id=workspace_id,
                preimage_sha256=digest,
                history_count=count,
            )
            for workspace_id, count, digest in histories
        )
    payload = {
        "version": 1,
        "preimage_format": "postgres-jsonb-row-sha256-v1",
        "binding": binding.model_dump(mode="json"),
        "identity": Identity.model_validate(gate.identity).model_dump(),
        **value.model_dump(),
        "prior_revision": revision,
        "initial": initial,
        "rows": [
            row.model_dump(mode="json")
            for row in sorted(projected, key=lambda row: (row.family, row.row_id))
        ],
    }
    return RestoreInventory.model_validate(
        {**payload, "inventory_sha256": canonical_sha256(payload)}
    ), records
