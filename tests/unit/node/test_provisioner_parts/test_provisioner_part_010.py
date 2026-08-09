"""Companion-secret event coverage split from the primary provisioner tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.repositories import SecretLeaseRepository, WorkspaceRepository
from awf.node.compose_manager import ComposeProjectPaths
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.unit.node.test_provisioner_parts.test_provisioner_part_001 import (
    git_manager,
    origin_repo,
    session_factory,
)

__all__ = ("git_manager", "origin_repo", "session_factory")


@pytest.mark.unit
async def test_companion_secret_metadata_event_persists_without_profile_secret_leases(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
) -> None:
    raw_secret = "sk-live-do-not-store-in-event"

    class _RecordingStackLauncher:
        async def launch(self, request: Any) -> object:
            return ComposeProjectPaths(
                project_dir=Path("/tmp/awf-compose/ws_companion"),
                compose_file=Path("/tmp/awf-compose/ws_companion/compose.yml"),
                secret_lease_mount_metadata={
                    "schema": "secret_lease_mount_metadata.v1",
                    "companion_env_secret_count": 1,
                    "companion_env_secrets": (
                        {
                            "companion": "reviewer",
                            "target": "ANTHROPIC_API_KEY",
                            "provider": "env",
                            "source": "ANTHROPIC_API_KEY",
                            "required": True,
                        },
                    ),
                    "companion_omitted_optional_env_secret_count": 1,
                    "companion_omitted_optional_env_secrets": (
                        {
                            "companion": "reviewer",
                            "target": "OPENAI_API_KEY",
                            "provider": "env",
                            "source": "OPENAI_API_KEY",
                            "required": False,
                        },
                    ),
                    "secret_value": raw_secret,
                },
            )

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=_RecordingStackLauncher(),
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            resolved_profile={"name": "companion-only-secrets"},
        )
        await s.commit()
        ws_id = ws.id

    await provisioner.provision(ws_id)

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(ws_id)
        assert reloaded is not None
        assert await SecretLeaseRepository(s).list_for_workspace(ws_id) == []
        events = [
            event
            for event in reloaded.events
            if event.event_type == "workspace.companion_env_secret_metadata"
        ]
        assert len(events) == 1
        event = events[0]
        assert event.reason_code == "COMPANION_ENV_SECRET_METADATA_RECORDED"
        assert event.payload == {
            "schema": "companion_env_secret_stack_metadata.v1",
            "compose_project": f"awf_{ws_id}",
            "compose_file": "/tmp/awf-compose/ws_companion/compose.yml",
            "companion_env_secret_count": 1,
            "companion_env_secrets": [
                {
                    "companion": "reviewer",
                    "target": "ANTHROPIC_API_KEY",
                    "provider": "env",
                    "source": "ANTHROPIC_API_KEY",
                    "required": True,
                }
            ],
            "companion_omitted_optional_env_secret_count": 1,
            "companion_omitted_optional_env_secrets": [
                {
                    "companion": "reviewer",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "source": "OPENAI_API_KEY",
                    "required": False,
                }
            ],
        }
        assert raw_secret not in json.dumps(event.payload, default=str)
