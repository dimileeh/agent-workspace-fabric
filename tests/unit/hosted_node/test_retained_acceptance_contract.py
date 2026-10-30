"""Bounded optional wire acceptance, and an additive old-schema migration."""

import asyncio
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from awf.db.session import make_engine
from awf.hosted_node.lifecycle_schemas import RecoveryAcceptance
from tests.postgres import postgres_empty_test_url
from tests.unit.hosted_node.test_lifecycle_migration import _migrate


def acceptance():
    return {
        "idempotency_key": "accepted",
        "receipt_ref": "fence",
        "previous_identity": None,
        "disk_ref": "disk",
        "database_ref": "db",
        "queued_workspaces": 0,
        "monitor_workspaces": 0,
        "attention_workspaces": 0,
    }


@pytest.mark.parametrize(
    "field", ["queued_workspaces", "monitor_workspaces", "attention_workspaces"]
)
@pytest.mark.parametrize("value", [-1, True, "1", 2**63])
def test_acceptance_counts_are_strict_and_bounded(field, value):
    with pytest.raises(ValidationError):
        RecoveryAcceptance.model_validate({**acceptance(), field: value})


def test_acceptance_does_not_accept_additional_authority():
    with pytest.raises(ValidationError):
        RecoveryAcceptance.model_validate({**acceptance(), "allow_replay": True})


async def test_additive_acceptance_migration_preserves_closed_old_gate():
    async with postgres_empty_test_url() as url:
        before = await asyncio.to_thread(_migrate, url, "upgrade", "db08e4f50617")
        assert before.returncode == 0, before.stderr.decode()[-2000:]
        engine = make_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("""INSERT INTO hosted_node_lifecycle
                    (singleton,identity,disk_ref,database_ref,phase,aborted_through,maintenance_generation)
                    VALUES (1,'{}','disk','db','recovering',0,7)""")
                )
            after = await asyncio.to_thread(_migrate, url, "upgrade", "head")
            assert after.returncode == 0, after.stderr.decode()[-2000:]
            async with engine.begin() as connection:
                row = (
                    (await connection.execute(text("SELECT * FROM hosted_node_lifecycle")))
                    .mappings()
                    .one()
                )
                assert row["phase"] == "recovering" and row["maintenance_generation"] == 7
                assert row["recovery_acceptance"] is None
                await connection.execute(
                    text(
                        "UPDATE hosted_node_lifecycle SET recovery_acceptance=CAST(:value AS jsonb)"
                    ),
                    {"value": json.dumps(acceptance())},
                )
            refused = await asyncio.to_thread(_migrate, url, "downgrade", "db08e4f50617")
            assert refused.returncode != 0 and b"NODE_LIFECYCLE_STILL_ENABLED" in refused.stderr
            async with engine.begin() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT recovery_acceptance FROM hosted_node_lifecycle")
                    )
                    == acceptance()
                )
                await connection.execute(text("DELETE FROM hosted_node_lifecycle"))
            after = await asyncio.to_thread(_migrate, url, "downgrade", "db08e4f50617")
            assert after.returncode == 0, after.stderr.decode()[-2000:]
        finally:
            await engine.dispose()
