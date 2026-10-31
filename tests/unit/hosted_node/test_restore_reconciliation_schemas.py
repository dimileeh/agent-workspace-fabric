"""Server-only restore evidence is bounded, canonical and default-denying."""

import importlib
from datetime import UTC, datetime

import pytest

from awf.hosted_node import restore_reconciliation_schemas as schema
from tests.unit.test_hosted_runtime_contract import hosted_create


def fact(action="workspace", outcome="unknown"):
    return {
        "action_id": action,
        "outcome": outcome,
        "evidence_ref": "synthetic-current-action",
        "evidence_sha256": "a" * 64,
        "observed_at": datetime.now(UTC),
        "external_action_ref": None if outcome == "unknown" else "current-action",
    }


def test_core_owns_canonical_nonsecret_reconciliation_hash():
    schema = importlib.import_module("awf.hosted_node.restore_reconciliation_schemas")
    assert schema.canonical_sha256({"b": 2, "a": 1}) == schema.canonical_sha256({"a": 1, "b": 2})
    assert schema.canonical_sha256({"a": 1}) != schema.canonical_sha256({"a": "1"})
    with pytest.raises(ValueError):
        schema.canonical_sha256({"a": float("nan")})


def test_known_action_cannot_be_an_unsubstantiated_boolean():
    schema = importlib.import_module("awf.hosted_node.restore_reconciliation_schemas")
    with pytest.raises(ValueError):
        schema.RestoreActionEvidence.model_validate(
            {"action_id": "workspace", "outcome": "never_started", "verified": True}
        )


def test_positive_action_requires_exact_external_reference():
    with pytest.raises(ValueError, match="RESTORE_KNOWN_ACTION_REQUIRED"):
        schema.RestoreActionEvidence.model_validate(
            {**fact(outcome="never_started"), "external_action_ref": None}
        )


@pytest.mark.parametrize("outcome", ["unknown", "never_started", "succeeded", "failed"])
def test_operation_fact_has_fixed_outcomes(outcome):
    assert (
        schema.RestoreOperationDecision(
            operation_id="operation", preimage_sha256="a" * 64, fact=fact("operation", outcome)
        ).fact.outcome
        == outcome
    )


@pytest.mark.parametrize(
    "changes",
    [{"action_id": "other"}, {"outcome": "monitor_safe", "external_action_ref": "action"}],
)
def test_operation_fact_denies_other_identity_or_kind(changes):
    with pytest.raises(ValueError, match="RESTORE_OPERATION_FACT_MISMATCH"):
        schema.RestoreOperationDecision(
            operation_id="operation",
            preimage_sha256="a" * 64,
            fact={**fact("operation"), **changes},
        )


@pytest.mark.parametrize(
    "fault",
    [
        "action",
        "outcome",
        "held_fresh",
        "held_operations",
        "missing_fresh",
        "wrong_fresh",
        "duplicate_operations",
        "unordered_operations",
    ],
)
def test_workspace_continuation_never_accepts_inconsistent_decisions(fault):
    hosted = hosted_create()["hosted"]
    workspace = hosted["workspace_id"]
    payload = {
        "workspace_id": workspace,
        "preimage_sha256": "a" * 64,
        "action_set_sha256": "b" * 64,
        "disposition": "held",
        "fact": fact(workspace),
    }
    operation = {
        "operation_id": "operation",
        "preimage_sha256": "c" * 64,
        "fact": fact("operation"),
    }
    if fault == "action":
        payload["fact"] = fact("different")
    elif fault == "outcome":
        payload["fact"] = fact(workspace, "never_started")
    elif fault == "held_fresh":
        payload["fresh_association"] = hosted
    elif fault == "held_operations":
        payload["operations"] = [operation]
    else:
        payload.update(
            disposition="queued", fact=fact(workspace, "never_started"), fresh_association=hosted
        )
        if fault == "missing_fresh":
            payload["fresh_association"] = None
        elif fault == "wrong_fresh":
            payload["workspace_id"] = "wrong"
            payload["fact"] = fact("wrong", "never_started")
        elif fault == "duplicate_operations":
            payload["operations"] = [operation, operation]
        else:
            payload["operations"] = [
                operation,
                {**operation, "operation_id": "a", "fact": fact("a")},
            ]
    with pytest.raises(ValueError, match="RESTORE_"):
        schema.RestoreWorkspaceDecision.model_validate(payload)


@pytest.mark.parametrize(
    "family,outcome",
    [
        ("tasks", "never_started"),
        ("callback_subscriptions", "current_routing"),
        ("callback_deliveries", "never_started"),
        ("service_gc_requests", "safe_gc"),
    ],
)
def test_orphan_decisions_bind_each_fixed_family(family, outcome):
    payload = {
        "family": family,
        "row_id": "row",
        "preimage_sha256": "a" * 64,
        "disposition": "resume",
        "fact": fact("row", outcome),
    }
    assert schema.RestoreRowDecision.model_validate(payload).disposition == "resume"
    with pytest.raises(ValueError, match="RESTORE_ROW_FACT_MISMATCH"):
        schema.RestoreRowDecision.model_validate({**payload, "fact": fact("different", outcome)})
    with pytest.raises(ValueError, match="RESTORE_ROW_FACT_MISMATCH"):
        schema.RestoreRowDecision.model_validate({**payload, "fact": fact("row")})
    if family != "service_gc_requests":
        with pytest.raises(ValueError, match="RESTORE_ROW_ID_INVALID"):
            schema.RestoreRowDecision.model_validate({**payload, "row_id": "r" * 37})
