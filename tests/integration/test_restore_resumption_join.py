"""Positive restore exit on actual Alembic schema and unprivileged Core SQL role."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from awf.hosted_node.host_database import validate_database
from awf.hosted_node.host_linux import LinuxObservation
from awf.hosted_node.host_runtime import HostRuntime
from tests.integration.test_checkpoint_restore_join import migrated_database as migrated_database
from tests.integration.test_closed_host_database import runtime_database as runtime_database
from tests.integration.test_closed_host_runtime import ack
from tests.integration.test_closed_host_runtime import composed as composed
from tests.integration.test_restore_material_staging import resumable_restore as resumable_restore
from tests.integration.test_restore_material_staging import (
    test_selected_existing_work_stages_before_resumption_and_new_admission as assert_positive,
)


@pytest.fixture
async def ready_node(composed, runtime_database, migrated_database):
    client, receiver, _, _ = composed
    assert (await ack(client)).status_code == 200
    return client, receiver, runtime_database[3]


async def test_actual_migrated_restricted_role_material_resume_and_new_work(
    resumable_restore,
    composed,
    runtime_database,
    monkeypatch,
):
    _, receiver, _, _, _ = resumable_restore
    _, _, prior_runtime, state = composed
    monkeypatch.setattr(LinuxObservation, "sample", lambda _self, **_kwargs: state["linux"])
    runtime = HostRuntime(
        receiver.config, receiver.factory, receiver.identity, prior_runtime.docker
    )
    runtime.baseline = state["linux"]
    receiver.lifecycle.observe_host = runtime.observe
    await assert_positive(resumable_restore)
    async with receiver.factory() as session:
        await validate_database(session, runtime_database[1])
        assert not await session.scalar(
            text(
                "SELECT rolcreaterole OR rolcreatedb OR rolsuper FROM pg_roles WHERE rolname=current_user"
            )
        )
    for sql in (
        "SET LOCAL session_replication_role=replica",
        "ALTER TABLE workspaces DISABLE TRIGGER ALL",
    ):
        with pytest.raises(DBAPIError):
            async with receiver.factory.begin() as session:
                await session.execute(text(sql))
