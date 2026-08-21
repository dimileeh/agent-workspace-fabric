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


@pytest.mark.unit
def test_mount_metadata_event_redacts_companion_secret_fields() -> None:
    """The mount-metadata event redacts ``companion_*`` fields like the companion event.

    Regression: ``_stack_secret_lease_mount_metadata`` copied ``companion_*``
    secret metadata verbatim while ``_stack_companion_env_secret_event_payload``
    redacted the same keys, so a sensitive value leaking into a ``companion_*``
    field would be exposed only in the broader mount-metadata event.
    """
    from awf.common.audit import REDACTION_MARKER
    from awf.node.provisioner_helpers import (
        _stack_companion_env_secret_event_payload,
        _stack_secret_lease_mount_metadata,
    )

    raw_token = "ghp_do_not_log_this"
    plan = {
        "schema": "secret_lease_mount_metadata.v1",
        "mount_plan": "profile_declared_secret_leases",
        "env_count": 1,
        "providers": ["env"],
        "targets": ["GH_TOKEN"],
        "companion_env_secret_count": 1,
        "companion_env_secrets": [
            {"companion": "reviewer", "target": "GH_TOKEN", "token": raw_token},
        ],
    }
    stack_paths = ComposeProjectPaths(
        project_dir=Path("/tmp/awf-compose/ws_redact"),
        compose_file=Path("/tmp/awf-compose/ws_redact/compose.yml"),
        secret_lease_mount_metadata=plan,
    )

    mount_md = _stack_secret_lease_mount_metadata(workspace_id="ws_redact", stack_paths=stack_paths)

    # The sensitive companion_* field is redacted; the raw token never appears.
    assert mount_md["companion_env_secrets"] == [
        {"companion": "reviewer", "target": "GH_TOKEN", "token": REDACTION_MARKER}
    ]
    assert raw_token not in json.dumps(mount_md, default=str)
    # Redaction is identical to the dedicated companion-secret event.
    companion_event = _stack_companion_env_secret_event_payload(
        workspace_id="ws_redact", stack_paths=stack_paths
    )
    assert companion_event is not None
    assert mount_md["companion_env_secrets"] == companion_event["companion_env_secrets"]
    # Non-companion fields stay verbatim (the else branch of the redaction guard).
    assert mount_md["providers"] == ["env"]
    assert mount_md["targets"] == ["GH_TOKEN"]
    assert mount_md["env_count"] == 1


@pytest.mark.unit
def test_workspace_supports_github_pull_head_ref_rejects_non_github_pr_url() -> None:
    """A Bitbucket PR URL must not select GitHub's synthetic pull-head ref."""
    from types import SimpleNamespace

    from awf.node.provisioner_helpers import _workspace_supports_github_pull_head_ref

    ws = SimpleNamespace(
        resolved_profile={"forge": "auto"},
        repo_url="git@github.com:x/y.git",
        pr_url="https://bitbucket.org/workspace/repo/pull-requests/1",
    )
    assert _workspace_supports_github_pull_head_ref(ws) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pr_url", "expected"),
    [
        (None, True),
        ("https://github.com/x/y/pull/1", True),
    ],
)
def test_workspace_supports_github_pull_head_ref_allows_github_or_missing_pr_url(
    pr_url: str | None,
    expected: bool,
) -> None:
    from types import SimpleNamespace

    from awf.node.provisioner_helpers import _workspace_supports_github_pull_head_ref

    ws = SimpleNamespace(
        resolved_profile={"forge": "github"},
        repo_url="git@github.com:x/y.git",
        pr_url=pr_url,
    )
    assert _workspace_supports_github_pull_head_ref(ws) is expected
