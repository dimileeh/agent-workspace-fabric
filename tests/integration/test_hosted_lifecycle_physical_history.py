"""Original terminal source through actual accepted replacement and later wakes."""

import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.db.models import Workspace
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.models import Owner, Receipt
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_schemas import HostedWorkspace
from tests.integration.test_hosted_lifecycle_reauthorization import (
    WS,
    install_pair,
    quiet_failed,
    wake,
)
from tests.unit.api.test_pr_monitor_adoption import _metadata
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


async def physical_wake(authority, create_application, create_client, monkeypatch):
    start = Receiver.start

    async def replacement(config, factory):
        return await start(
            config.model_copy(
                update={
                    "incarnation": "8063738394745155542",
                    "recovery_generation": config.recovery_generation + 1,
                }
            ),
            factory,
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(Receiver, "start", replacement)
        return await wake(authority, create_application, create_client)


@pytest.mark.parametrize("later_wakes", [0, 2])
@pytest.mark.parametrize("action", ["retry", "adopt-pr", "guide", "remonitor"])
async def test_terminal_source_survives_accepted_physical_repair(
    authority, create_client, create_application, tmp_path, monkeypatch, later_wakes, action
):
    await quiet_failed(authority, create_client, tmp_path)
    original = HostedWorkspace.model_validate(authority[1]["hosted"]).model_dump(mode="json")
    runtime = None
    try:
        runtime = await physical_wake(authority, create_application, create_client, monkeypatch)
        assert runtime.receiver.identity.incarnation != authority[0].receiver.identity.incarnation
        for _ in range(later_wakes):
            runtime = await wake(authority, create_application, create_client)
        target_id = "ws_after_repair" if action in {"retry", "adopt-pr"} else WS
        current = await install_pair(
            *create_client, original, runtime.receiver.identity, workspace_id=target_id
        )
        observed = []

        async def forge(self, args, **kwargs):
            observed.append(kwargs["env"]["GH_TOKEN"])
            return CommandResult(
                returncode=0,
                stderr="",
                stdout=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "merged": False,
                                    "closed": False,
                                    "headRefName": "feature/existing",
                                    "headRefOid": "a" * 40,
                                    "baseRefOid": "b" * 40,
                                }
                            }
                        },
                    }
                ),
            )

        monkeypatch.setattr(AsyncioSubprocessRunner, "run", forge)
        if action == "retry":
            path, body = f"/v1/workspaces/{WS}/retry", {"hosted": current}
        elif action == "adopt-pr":
            create_application[0].state.pr_adoption_metadata_fetcher = AsyncMock(
                return_value=replace(
                    _metadata(number=42),
                    head_repo_slug="example/repo",
                    url="https://github.com/example/repo/pull/42",
                )
            )
            path, body = (
                "/v1/workspaces/adopt-pr",
                {
                    "pr_url": "https://github.com/example/repo/pull/42",
                    "repo_url": "https://github.com/example/repo.git",
                    "source_workspace_id": WS,
                    "hosted": current,
                },
            )
        else:
            path, body = (
                f"/v1/workspaces/{WS}/{action}",
                {
                    "reason": "Current caller after accepted repair",
                    "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
                    "hosted_reauthorization": {
                        "expected_association": deepcopy(original),
                        "replacement_association": current,
                    },
                    **({"directive": "Continue review"} if action == "guide" else {}),
                },
            )
        headers = {**create_client[1], "Idempotency-Key": "accepted-repair"}
        response = await create_client[0].post(path, json=body, headers=headers)
        assert response.status_code == (202 if target_id != WS else 200), response.text
        replay = await create_client[0].post(path, json=body, headers=headers)
        assert replay.status_code == response.status_code, replay.text
        key = "new_workspace_id" if action == "retry" else "workspace_id"
        assert replay.json()[key] == response.json()[key] == target_id
        async with authority[2]() as session:
            row = await session.get(Workspace, target_id)
            assert row.task_policy["hosted_node"] == current
            if target_id != WS:
                source = await session.get(Workspace, WS)
                assert source.task_policy["hosted_node"] == original
        assert (await runtime.workspace(target_id)).environment["GH_TOKEN"] == "fresh-github"
        if action == "retry":
            assert observed == ["fresh-github"]
    finally:
        if runtime is not None:
            await runtime.receiver.close()


@pytest.mark.parametrize(
    "fault", ["acceptance", "storage", "unacknowledged", "receipt", "attention"]
)
async def test_replacement_history_still_requires_durable_exact_proof(
    authority, create_client, create_application, tmp_path, monkeypatch, fault
):
    from sqlalchemy import select

    await quiet_failed(authority, create_client, tmp_path)
    expected = HostedWorkspace.model_validate(authority[1]["hosted"]).model_dump(mode="json")
    runtime = await physical_wake(authority, create_application, create_client, monkeypatch)
    try:
        replacement = await install_pair(*create_client, expected, runtime.receiver.identity)
        async with authority[2].begin() as session:
            if fault == "unacknowledged":
                owner = await session.get(Owner, runtime.receiver.identity.owner)
                owner.acknowledgement = None
            elif fault == "receipt":
                receipt = await session.scalar(
                    select(Receipt).where(Receipt.idempotency_key == "agent")
                )
                receipt.binding = {**receipt.binding, "namespace": "foreign"}
            elif fault == "attention":
                row = await session.get(Workspace, WS)
                row.task_policy = {
                    **row.task_policy,
                    "hosted_recovery": {"disposition": "attention"},
                }
            else:
                gate = await session.get(NodeLifecycleRow, 1)
                if fault == "acceptance":
                    gate.recovery_acceptance = None
                else:
                    gate.disk_ref = "unaccepted-disk"
        response = await create_client[0].post(
            f"/v1/workspaces/{WS}/guide",
            json={
                "directive": "Continue review",
                "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
                "hosted_reauthorization": {
                    "expected_association": expected,
                    "replacement_association": replacement,
                },
            },
            headers={**create_client[1], "Idempotency-Key": "unproven-history"},
        )
        assert response.status_code in {403, 409}, response.text
        async with authority[2]() as session:
            row = await session.get(Workspace, WS)
            assert row.task_policy["hosted_node"] == expected
            assert row.pending_operator_hint is None
    finally:
        await runtime.receiver.close()
