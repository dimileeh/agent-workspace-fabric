"""Hosted profile-secret declaration outcomes at provisioning time."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.profile_secrets import HostedProfileSecretError
from awf.node.compose_manager import ComposeProjectPaths
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.node.provisioner_helpers import _stack_hosted_skipped_secret_event_payload
from awf.profiles.resolver import ProfileResolutionError
from tests.unit.node.test_provisioner_parts.test_provisioner_part_001 import (
    git_manager,
    origin_repo,
    session_factory,
)

__all__ = ("git_manager", "origin_repo", "session_factory")

_SKIPPED_EVENT = "workspace.hosted_profile_secret_skipped"


def _stack_paths(metadata: dict[str, object]) -> ComposeProjectPaths:
    return ComposeProjectPaths(
        project_dir=Path("/tmp/awf-compose/ws_hosted"),
        compose_file=Path("/tmp/awf-compose/ws_hosted/compose.yml"),
        secret_lease_mount_metadata=metadata,
    )


class _HostedStackLauncher:
    def __init__(self, metadata: dict[str, object]) -> None:
        self.metadata = metadata

    async def launch(self, request: Any) -> object:
        return _stack_paths(self.metadata)


async def _create_workspace(
    session_factory: async_sessionmaker[AsyncSession], origin_repo: Path
) -> str:
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            resolved_profile={"name": "hosted-profile-secrets"},
        )
        await s.commit()
        return ws.id


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason_code", ["PROTECTED_DECLARATION_UNSUPPORTED", "PROTECTED_DECLARATION_MISMATCH"]
)
async def test_hosted_profile_secret_error_fails_as_non_transient_profile_resolution(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
    reason_code: str,
) -> None:
    launches: list[Any] = []

    class _RefusingStackLauncher:
        async def launch(self, request: Any) -> object:
            launches.append(request)
            raise HostedProfileSecretError(reason_code, "local_db_password")

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=_RefusingStackLauncher(),
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    ws_id = await _create_workspace(session_factory, origin_repo)

    with pytest.raises(ProfileResolutionError):
        await provisioner.provision(ws_id)

    assert len(launches) == 1
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(ws_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "profile_resolution_failure"
        # Refused before Compose I/O: the pre-launch project name must not
        # linger as a host-port blocker on the failed row.
        assert reloaded.compose_project_name is None
        assert reloaded.failure_message is not None
        assert reloaded.failure_message.startswith(f"{reason_code}:")
        assert "'local_db_password'" in reloaded.failure_message
        assert "unexpected provisioning failure" not in reloaded.failure_message
        assert any(event.reason_code == reason_code for event in reloaded.events)
        assert not [event for event in reloaded.events if event.event_type == _SKIPPED_EVENT]


@pytest.mark.unit
async def test_hosted_skipped_optional_secrets_record_one_names_only_event(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
) -> None:
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=_HostedStackLauncher(
            {
                "protected_mount_count": 2,
                "hosted_skipped_optional_secrets": ["local_db_password", "local_cache_token"],
            }
        ),
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    ws_id = await _create_workspace(session_factory, origin_repo)

    await provisioner.provision(ws_id)

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(ws_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        events = [event for event in reloaded.events if event.event_type == _SKIPPED_EVENT]
        assert len(events) == 1
        assert events[0].reason_code == "HOSTED_OPTIONAL_SECRET_SKIPPED"
        assert events[0].payload == {
            "schema": "hosted_profile_secret_skipped.v1",
            "secret_names": ["local_db_password", "local_cache_token"],
            "count": 2,
        }


@pytest.mark.unit
async def test_hosted_stack_without_skipped_secrets_records_no_skip_event(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
) -> None:
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=_HostedStackLauncher({"protected_mount_count": 2}),
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    ws_id = await _create_workspace(session_factory, origin_repo)

    await provisioner.provision(ws_id)

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(ws_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert not [event for event in reloaded.events if event.event_type == _SKIPPED_EVENT]


@pytest.mark.unit
def test_skipped_secret_event_payload_is_names_only() -> None:
    absent = _stack_paths({"protected_mount_count": 2})
    empty = _stack_paths({"hosted_skipped_optional_secrets": []})
    assert _stack_hosted_skipped_secret_event_payload(stack_paths=absent) is None
    assert _stack_hosted_skipped_secret_event_payload(stack_paths=empty) is None

    payload = _stack_hosted_skipped_secret_event_payload(
        stack_paths=_stack_paths(
            {
                "hosted_skipped_optional_secrets": ("db_password",),
                "secret_value": "do-not-copy-into-event",
            }
        )
    )

    assert payload == {
        "schema": "hosted_profile_secret_skipped.v1",
        "secret_names": ["db_password"],
        "count": 1,
    }
    assert "do-not-copy-into-event" not in json.dumps(payload)
