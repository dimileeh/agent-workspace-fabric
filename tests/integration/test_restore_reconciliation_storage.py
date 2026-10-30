"""Restore journals/holds are actual additive SQL, not process-local authority."""

import pytest
from sqlalchemy import text

from awf.db.session import make_session_factory
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_barrier import install_barrier, verify_barrier


async def test_restore_journal_and_fixed_family_holds_are_registered(engine):
    async with engine.connect() as connection:
        for table in ("hosted_restore_reconciliations", "hosted_restore_row_holds"):
            assert await connection.scalar(text("SELECT to_regclass(:name)"), {"name": table})
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() "
                    "AND table_name='hosted_node_lifecycle' AND column_name='restore_reconciliation_id'"
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    "table", ["hosted_node_lifecycle", "hosted_restore_row_holds", "hosted_restore_reconciliations"]
)
async def test_runtime_validation_rejects_missing_restore_metadata_guard(engine, table):
    async with engine.begin() as connection:
        await connection.run_sync(install_barrier)
        await connection.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER awf_restore_metadata"))
    async with make_session_factory(engine)() as session:
        with pytest.raises(MaterialDeniedError, match="NODE_BARRIER_UNAVAILABLE"):
            await verify_barrier(session)
