"""Physical repair history never implies authority over a rewound terminal row."""

from datetime import UTC, datetime, timedelta

import pytest

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_reauthorization import require_nonrewound_history
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_quarantine import restore
from tests.integration.test_restore_reconciliation import unknown_decisions


async def test_actual_committed_restore_journal_bounds_subsequent_source_history(ready_node):
    client, receiver, _, binding = await restore(ready_node)
    accepted = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    assert accepted.status_code == 200, accepted.text

    decision = None

    async def current(inventory):
        nonlocal decision
        if decision is None:
            decision = unknown_decisions(inventory)
        return decision.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=10)})

    receiver.lifecycle.verify_restore_reconciliation = current
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": "reconcile-history", "receipt_ref": "current-facts"},
    )
    assert response.status_code == 200, response.text
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.restore_quarantine is None and gate.restore_reconciliation_id
        with pytest.raises(MaterialDeniedError, match="RECOVERY_ATTENTION_REQUIRED"):
            await require_nonrewound_history(session, gate, binding.checkpoint.identity)
        await require_nonrewound_history(session, gate, receiver.identity)
        for change in (
            {"owner": "foreign"},
            {"boot_epoch": receiver.identity.boot_epoch - 1},
            {"recovery_generation": receiver.identity.recovery_generation - 1},
            {"incarnation": "foreign"},
        ):
            with pytest.raises(MaterialDeniedError):
                await require_nonrewound_history(
                    session, gate, receiver.identity.model_copy(update=change)
                )
        await require_nonrewound_history(
            session,
            gate,
            receiver.identity.model_copy(
                update={
                    "boot_epoch": receiver.identity.boot_epoch + 2,
                    "recovery_generation": receiver.identity.recovery_generation + 1,
                    "incarnation": "later-replacement",
                }
            ),
        )
