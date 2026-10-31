"""Only bound restore attention extends the existing hosted projection."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from awf.service.workspace_observability import workspace_attention_fields


@pytest.mark.parametrize(
    "policy,node_id",
    [
        (None, None),
        ({}, None),
        ({"hosted_recovery": {"disposition": "attention"}}, None),
        ({"hosted_recovery": {"disposition": "attention", "restore_id": "restore"}}, "node"),
        (
            {
                "hosted_recovery": {
                    "disposition": "attention",
                    "restore_id": "restore",
                    "identity": {"owner": "other"},
                }
            },
            "node",
        ),
        (
            {
                "hosted_recovery": {
                    "disposition": "attention",
                    "restore_id": "restore",
                    "identity": {"owner": None},
                }
            },
            None,
        ),
    ],
)
def test_unmarked_local_and_unbound_restore_metadata_do_not_create_attention(policy, node_id):
    row = SimpleNamespace(status="running", task_policy=policy, node_id=node_id)
    assert workspace_attention_fields(row) == {
        "attention_required": False,
        "awaiting_human_since": None,
        "awaiting_human_reason": None,
    }


def test_bound_restore_preserves_existing_monitor_human_episode():
    since = datetime(2026, 10, 1, tzinfo=UTC)
    row = SimpleNamespace(
        status="monitoring_pr",
        node_id="node",
        awaiting_human_since=since,
        awaiting_human_reason="Existing decision",
        task_policy={
            "hosted_recovery": {
                "disposition": "attention",
                "restore_id": "restore",
                "identity": {"owner": "node"},
            }
        },
    )
    assert workspace_attention_fields(row) == {
        "attention_required": True,
        "awaiting_human_since": since,
        "awaiting_human_reason": "Existing decision",
    }
