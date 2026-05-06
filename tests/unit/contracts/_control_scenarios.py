"""Executable scenarios for registry-driven workspace control contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.api.routes.controls as controls_route
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from tests.unit.contracts._capabilities import CAPABILITIES_BY_NAME
from tests.unit.contracts._stack import ContractStack

CONTROL_CAPABILITY_NAMES = (
    "cancel_workspace",
    "stop_workspace",
    "destroy_workspace",
    "remonitor_workspace",
    "request_validation",
    "refresh_workspace",
    "rebase_workspace",
)


async def seed_workspace_for_control(
    factory: async_sessionmaker[AsyncSession],
    capability_name: str,
) -> tuple[str, int]:
    if capability_name in {"cancel_workspace", "stop_workspace", "destroy_workspace"}:
        return await seed_basic_workspace(factory)
    if capability_name == "refresh_workspace":
        return await seed_monitoring_workspace(factory, final_status=WorkspaceStatus.ready)
    if capability_name == "rebase_workspace":
        return await seed_monitoring_workspace(factory, with_open_candidate=True)
    if capability_name in {"remonitor_workspace", "request_validation"}:
        return await seed_monitoring_workspace(factory)
    raise AssertionError(f"unknown control capability {capability_name}")


async def seed_basic_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[str, int]:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/contract-controls.git",
            branch_base="main",
            task_title="Control contract",
            task_prompt="Exercise registry-driven control parity.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await session.commit()
        return workspace.id, workspace.version


async def seed_monitoring_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    with_pr_url: bool = True,
    with_open_candidate: bool = False,
    final_status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
) -> tuple[str, int]:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/contract-monitor.git",
            branch_base="main",
            task_title="Monitoring contract",
            task_prompt="Exercise registry-driven monitor controls.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.remote_push_branch = workspace.branch_name
        workspace.base_commit = "a" * 40
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = f"/tmp/awf/{workspace.id}/compose.yml"
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        if final_status == WorkspaceStatus.ready:
            await session.commit()
            return workspace.id, workspace.version
        if final_status in {WorkspaceStatus.destroying, WorkspaceStatus.destroyed}:
            await repo.transition(workspace, to=WorkspaceStatus.destroying, reason_code="SEED")
            if final_status == WorkspaceStatus.destroyed:
                await repo.transition(workspace, to=WorkspaceStatus.destroyed, reason_code="SEED")
            await session.commit()
            return workspace.id, workspace.version

        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.pushing, reason_code="SEED")
        if with_pr_url:
            workspace.pr_url = "https://github.com/example/contract-monitor/pull/42"
            workspace.pr_number = 42
            workspace.monitor_last_commit_sha = "b" * 40
        await repo.transition(workspace, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        if final_status in {
            WorkspaceStatus.completed,
            WorkspaceStatus.cancelled,
            WorkspaceStatus.failed,
        }:
            await repo.transition(workspace, to=final_status, reason_code="SEED")
        elif final_status != WorkspaceStatus.monitoring_pr:
            raise AssertionError(f"unsupported seed status {final_status}")

        if with_open_candidate:
            await MergeCandidateRepository(session).create_or_update_open_for_attempt(
                task=task,
                attempt=attempt,
                workspace=workspace,
                head_sha=workspace.monitor_last_commit_sha or ("b" * 40),
                base_sha=workspace.base_commit or ("a" * 40),
            )
        await session.commit()
        return workspace.id, workspace.version


def install_control_side_effect_stubs(
    contract_stack: ContractStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_stop(_compose_project_name: str | None) -> None:
        return None

    class FakeCleaner:
        async def cleanup(
            self,
            *,
            workspace_id: str,
            repo_url: str,
            remove_volumes: bool,
            remove_worktree: bool,
            compose_project_name: str | None = None,
            compose_file_path: Path | None = None,
            worktree_host_path: Path | None = None,
        ) -> list[str]:
            return []

    monkeypatch.setattr(controls_route, "_stop_project", fake_stop)
    monkeypatch.setattr(controls_route, "_cleaner", FakeCleaner)
    contract_stack.service._project_stopper = fake_stop  # type: ignore[attr-defined]
    contract_stack.service._cleaner_factory = FakeCleaner  # type: ignore[attr-defined]


async def call_rest_control(
    contract_stack: ContractStack,
    capability_name: str,
    *,
    workspace_id: str,
    idempotency_key: str,
    expected_version: int | None = None,
    variant: str = "base",
) -> Any:
    capability = CAPABILITIES_BY_NAME[capability_name]
    headers = {
        **contract_stack.auth_headers,
        "Idempotency-Key": idempotency_key,
    }
    if expected_version is not None:
        headers["If-Match"] = str(expected_version)
    return await contract_stack.client.request(
        capability.rest_method,
        capability.rest_path.format(workspace_id=workspace_id),
        headers=headers,
        params=control_rest_params(capability_name, variant=variant),
        json=control_rest_body(capability_name, variant=variant),
    )


async def call_mcp_control(
    contract_stack: ContractStack,
    capability_name: str,
    *,
    workspace_id: str,
    idempotency_key: str,
    expected_version: int | None = None,
    variant: str = "base",
) -> Any:
    capability = CAPABILITIES_BY_NAME[capability_name]
    args = control_mcp_args(
        capability_name,
        workspace_id=workspace_id,
        idempotency_key=idempotency_key,
        expected_version=expected_version,
        variant=variant,
    )
    return await contract_stack.mcp.call_tool(capability.mcp_tool or "", args)


def control_rest_body(capability_name: str, *, variant: str = "base") -> dict[str, object] | None:
    reason = "operator recovery" if variant == "base" else "changed operator recovery"
    if capability_name == "cancel_workspace":
        return {"reason": reason, "stop_stack": variant == "base"}
    if capability_name in {"stop_workspace", "remonitor_workspace", "refresh_workspace", "rebase_workspace"}:
        return {"reason": reason}
    if capability_name == "request_validation":
        return {
            "reason": reason,
            "requested_tier": 2 if variant == "base" else 3,
        }
    if capability_name == "destroy_workspace":
        return None
    raise AssertionError(f"unknown control capability {capability_name}")


def control_rest_params(capability_name: str, *, variant: str = "base") -> dict[str, object]:
    if capability_name != "destroy_workspace":
        return {}
    return {
        "force": True,
        "remove_volumes": variant != "base",
        "remove_worktree": False,
    }


def control_mcp_args(
    capability_name: str,
    *,
    workspace_id: str,
    idempotency_key: str,
    expected_version: int | None = None,
    variant: str = "base",
) -> dict[str, object]:
    args: dict[str, object] = {
        "workspace_id": workspace_id,
        "idempotency_key": idempotency_key,
    }
    if expected_version is not None:
        args["expected_version"] = expected_version

    body = control_rest_body(capability_name, variant=variant) or {}
    args.update(body)
    if capability_name == "destroy_workspace":
        args.update(control_rest_params(capability_name, variant=variant))
    return args


def control_success_status(capability_name: str) -> int:
    return 202 if capability_name in {"request_validation", "refresh_workspace", "rebase_workspace"} else 200


def response_operation_id_field(capability_name: str) -> str:
    return "id" if capability_name in {"request_validation", "refresh_workspace", "rebase_workspace"} else "operation_id"


def unique_key(capability_name: str, suffix: str) -> str:
    return f"{capability_name.replace('_', '-')}-{suffix}-{datetime.now(UTC).timestamp()}"
