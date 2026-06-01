"""Regression test for intra-workspace companion-vs-profile host-port conflict.

``_check_auto_resolved_profile_host_ports`` excludes the current workspace
from ``find_host_port_conflicts`` via ``excluding_workspace_id``, which is
correct for cross-workspace conflict detection.  However, when a companion
and an auto-resolved profile service **within the same workspace** claim the
same host port, the self-exclusion hides the conflict, leading to a Docker
Compose INFRASTRUCTURE_FAILURE at launch instead of a clean
DUPLICATE_HOST_PORT rejection.

The fix adds a pre-DB in-memory check that compares profile ports against
the workspace's own companion ports before the cross-workspace DB query.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.profiles.models import WorkspaceProfile
from awf.service.workspaces import WorkspaceCreateDuplicateHostPortError


def _profile_with_service_port(host_port: int) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "test-profile",
            "docker": {"mode": "dind"},
            "services": [
                {
                    "name": "db",
                    "image": "postgres:16",
                    "ports": [[5432, host_port]],
                }
            ],
            "security": {"egress": {"mode": "restricted"}},
        }
    )


def _companion_task_policy(*host_ports: int) -> dict[str, Any]:
    return {
        "companions": [
            {
                "name": f"svc-{i}",
                "repo_url": f"git@github.com:example/svc-{i}.git",
                "ports": [[80, hp]],
            }
            for i, hp in enumerate(host_ports)
        ]
    }


def _profile_with_duplicate_service_ports(host_port: int) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "test-profile",
            "docker": {"mode": "dind"},
            "services": [
                {
                    "name": "db",
                    "image": "postgres:16",
                    "ports": [[5432, host_port]],
                },
                {
                    "name": "cache",
                    "image": "redis:7",
                    "ports": [[6379, host_port]],
                },
            ],
            "security": {"egress": {"mode": "restricted"}},
        }
    )


class TestAutoResolvedProfileIntraProfileDuplicate:
    """Regression: two profile services claiming the same host port."""

    @pytest.mark.asyncio
    async def test_duplicate_detected(self) -> None:
        session_factory = AsyncMock()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=AsyncMock(),
            stack_launcher=None,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        profile = _profile_with_duplicate_service_ports(8080)
        task_policy: dict[str, Any] = {"companions": []}
        with pytest.raises(WorkspaceCreateDuplicateHostPortError) as exc_info:
            await provisioner._check_auto_resolved_profile_host_ports(
                workspace_id="ws-1",
                profile=profile,
                excluding_workspace_id="ws-1",
                task_policy=task_policy,
            )
        assert exc_info.value.host_port == 8080
        assert session_factory.call_count == 0


class TestAutoResolvedProfileIntraWorkspaceDuplicate:
    """Regression: companion and profile sharing a host port within the same workspace."""

    @pytest.mark.asyncio
    async def test_duplicate_detected(self) -> None:
        session_factory = AsyncMock()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=AsyncMock(),
            stack_launcher=None,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        profile = _profile_with_service_port(8080)
        task_policy = _companion_task_policy(8080)
        with pytest.raises(WorkspaceCreateDuplicateHostPortError) as exc_info:
            await provisioner._check_auto_resolved_profile_host_ports(
                workspace_id="ws-1",
                profile=profile,
                excluding_workspace_id="ws-1",
                task_policy=task_policy,
            )
        assert exc_info.value.host_port == 8080
        assert session_factory.call_count == 0

    @pytest.mark.asyncio
    async def test_no_duplicate_when_profile_ports_differ_from_companions(
        self,
    ) -> None:
        session_factory = AsyncMock()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=AsyncMock(),
            stack_launcher=None,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        profile = _profile_with_service_port(5432)
        task_policy = _companion_task_policy(8080, 9090)
        with pytest.raises(TypeError):
            await provisioner._check_auto_resolved_profile_host_ports(
                workspace_id="ws-1",
                profile=profile,
                excluding_workspace_id="ws-1",
                task_policy=task_policy,
            )
        assert session_factory.call_count == 1

    @pytest.mark.asyncio
    async def test_no_check_when_profile_has_no_host_ports(self) -> None:
        session_factory = AsyncMock()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=AsyncMock(),
            stack_launcher=None,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "empty-profile",
                "services": [],
                "security": {"egress": {"mode": "restricted"}},
            }
        )
        task_policy = _companion_task_policy(8080)
        await provisioner._check_auto_resolved_profile_host_ports(
            workspace_id="ws-1",
            profile=profile,
            excluding_workspace_id="ws-1",
            task_policy=task_policy,
        )
        assert session_factory.call_count == 0
