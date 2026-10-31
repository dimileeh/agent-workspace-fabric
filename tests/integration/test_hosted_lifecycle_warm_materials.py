"""Fresh same-boot association reaches the retained directory before invocation."""

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

from awf.db.models import Workspace, WorkspaceEvent
from awf.hosted_node.identity import WorkspaceScopeDeniedError
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Binding
from awf.profiles.models import WorkspaceProfile
from tests.integration.test_hosted_lifecycle_reauthorization import WS, install_pair, quiet_failed
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("action", ["guide", "remonitor"])
@pytest.mark.parametrize("cleared", [False, True])
@pytest.mark.parametrize("revoke_fresh", [False, True])
async def test_paused_reauthorization_materials_reach_existing_directory_before_consumer(
    authority, create_client, tmp_path, action, cleared, revoke_fresh
):
    runtime, value, factory = authority
    await quiet_failed(authority, create_client, tmp_path)
    async with factory.begin() as session:
        row = await session.get(Workspace, WS)
        row.status = "monitoring_pr"
        row.awaiting_human_since = datetime.now(UTC)
        row.awaiting_human_reason = "Review decision"
        await session.execute(
            delete(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == WS,
                WorkspaceEvent.event_type == "workspace.terminal_runtime_released",
            )
        )
    mounts = await runtime.profile_mounts(WorkspaceProfile(name="synthetic"), WS)
    assert mounts.mounts[0].target == "/run/awf/secrets"
    assert mounts.mounts[0].mode == "ro"
    directory = os.open(mounts.mounts[0].source, os.O_RDONLY | os.O_DIRECTORY)
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()

    def read(name):
        with os.fdopen(os.open(name, os.O_RDONLY, dir_fd=directory), "rb") as stream:
            return stream.read()

    try:
        inode = os.fstat(directory).st_ino
        assert read("github.api-key") == b"synthetic-github-key"
        if cleared:
            await consumers._stop(WS, authority_lost=False)
            assert os.listdir(directory) == []  # noqa: PTH208 - retained directory fd
            consumers.stop_workspace.reset_mock()
        expected = HostedWorkspace.model_validate(value["hosted"]).model_dump(mode="json")
        for role, binding in expected["credentials"].items():
            assert (
                await runtime.receiver.revoke(
                    Binding.model_validate(
                        {**binding, "idempotency_key": f"retire-{role}", "fence": 2}
                    )
                )
            ).status == "revoked"
        replacement = await install_pair(*create_client, expected, runtime.receiver.identity)
        body = {
            "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
            "hosted_reauthorization": {
                "expected_association": expected,
                "replacement_association": replacement,
            },
            **({"directive": "Continue review"} if action == "guide" else {}),
        }
        response = await create_client[0].post(
            f"/v1/workspaces/{WS}/{action}",
            json=body,
            headers={**create_client[1], "Idempotency-Key": "warm-reauthorization"},
        )
        assert response.status_code == 200, response.text
        # The API acknowledgement does not claim file publication or execution.
        if cleared:
            assert os.listdir(directory) == []  # noqa: PTH208 - retained directory fd
        else:
            assert read("github.api-key") == b"synthetic-github-key"
        if revoke_fresh:
            binding = replacement["credentials"]["agent"]
            await runtime.receiver.revoke(
                Binding.model_validate({**binding, "idempotency_key": "revoked-new", "fence": 2})
            )
            with pytest.raises(WorkspaceScopeDeniedError):
                async with consumers.scope(WS):
                    pytest.fail("Revoked current authority reached invocation")
            consumers.stop_workspace.assert_awaited_once_with(WS)
            assert os.listdir(directory) == []  # noqa: PTH208 - retained directory fd
        else:
            async with consumers.scope(WS):
                assert Path(mounts.mounts[0].source).stat().st_ino == inode
                assert read("github.api-key") == b"fresh-github"
                assert read("openai.api-key") == b"fresh-agent"
                assert (await consumers.environment())["GH_TOKEN"] == "fresh-github"
            consumers.stop_workspace.assert_not_awaited()
    finally:
        os.close(directory)
