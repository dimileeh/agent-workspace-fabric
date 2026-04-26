"""Launch per-workspace service stacks from resolved workspace profiles."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from awf.common.git_identity import DEFAULT_GIT_AUTHOR_EMAIL, DEFAULT_GIT_AUTHOR_NAME
from awf.node.auth_mounts import WorkspaceAuthMountResolver
from awf.node.compose_manager import (
    AuthMount,
    ComposeManager,
    ComposeProjectPaths,
    WorkspaceComposeSpec,
)
from awf.node.git_manager import WorktreeLayout
from awf.profiles.compose import profile_agent_environment, profile_services
from awf.profiles.models import WorkspaceProfile


@dataclass(frozen=True)
class WorkspaceStackLaunchRequest:
    """Inputs required to launch a workspace's outer Compose stack."""

    workspace_id: str
    layout: WorktreeLayout
    profile: WorkspaceProfile


class WorkspaceStackLauncher(Protocol):
    """Small seam for provisioning tests to avoid requiring Docker."""

    async def launch(self, request: WorkspaceStackLaunchRequest) -> ComposeProjectPaths: ...


class ComposeStackLauncher:
    """Render and start the profile-driven outer workspace Compose stack."""

    def __init__(
        self,
        *,
        compose: ComposeManager,
        agent_runtime_image: str,
        auth_mount_resolver: WorkspaceAuthMountResolver | None = None,
    ) -> None:
        self._compose = compose
        self._agent_runtime_image = agent_runtime_image
        self._auth_mount_resolver = auth_mount_resolver

    async def launch(self, request: WorkspaceStackLaunchRequest) -> ComposeProjectPaths:
        layout = request.layout
        profile = request.profile
        # Linked git worktrees store writable refs/objects in the common mirror.
        # Agents need that metadata writable when they make local commits.
        mirror_mount = AuthMount(
            source=str(layout.mirror_path),
            target=str(layout.mirror_path),
            mode="rw",
        )
        auth_mounts = [mirror_mount]
        if self._auth_mount_resolver is not None:
            auth_mounts.extend(
                await asyncio.to_thread(
                    self._auth_mount_resolver.resolve,
                    workspace_id=request.workspace_id,
                )
            )
        spec = WorkspaceComposeSpec(
            workspace_id=request.workspace_id,
            worktree_host_path=layout.worktree_path,
            agent_runtime_image=self._agent_runtime_image,
            agent_environment=profile_agent_environment(profile),
            docker_mode=profile.docker.mode.value,
            services=profile_services(profile),
            auth_mounts=tuple(auth_mounts),
            git_name=DEFAULT_GIT_AUTHOR_NAME,
            git_email=DEFAULT_GIT_AUTHOR_EMAIL,
        )
        return await self._compose.up(spec, wait=True)
