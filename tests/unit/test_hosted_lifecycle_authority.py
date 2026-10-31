"""Hosted extensions do not reinterpret ordinary legacy control rows."""

import pytest

from awf.db.models import Workspace
from awf.db.session import make_session_factory
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_operations import control_authority
from awf.hosted_node.runtime_schemas import HostedControlAuthorization


@pytest.mark.parametrize("policy", [None, "legacy", {}])
async def test_local_control_does_not_require_hosted_authority(engine, policy):
    factory = make_session_factory(engine)
    async with factory.begin() as session:
        row = Workspace(
            id="ws_legacy",
            status="requested",
            agent="codex",
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="Legacy",
            task_prompt="Legacy",
            task_policy=policy,
        )
        session.add(row)
        await session.flush()
        await control_authority(session, row.id, None)
        assert row.task_policy == policy


async def test_local_service_cannot_accept_controller_attestation(engine):
    async with make_session_factory(engine)() as session:
        with pytest.raises(MaterialDeniedError, match="HOSTED_RUNTIME_INTEGRATION_REQUIRED"):
            await control_authority(
                session,
                "ws_missing",
                HostedControlAuthorization(namespace="tenant", actor="caller"),
            )
