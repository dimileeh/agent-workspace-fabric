"""Launch per-workspace service stacks from resolved workspace profiles."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Protocol

from awf.common.git_identity import DEFAULT_GIT_AUTHOR_EMAIL, DEFAULT_GIT_AUTHOR_NAME
from awf.node.auth_mounts import WorkspaceAuthMountResolver, legacy_provider_targets
from awf.node.companion_images import CompanionImageBuilder
from awf.node.companion_services import (
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
    companion_env_secret_stack_metadata,
    companion_service_from_materialized,
    validate_companion_service_graph,
)
from awf.node.compose_manager import (
    AuthMount,
    CompanionService,
    ComposeManager,
    ComposeOperationError,
    ComposeProjectPaths,
    WorkspaceComposeSpec,
    compose_up_capture_timeout_seconds,
)
from awf.node.egress_policy import local_egress_plan
from awf.node.git_manager import WorktreeLayout
from awf.node.secret_mounts import LocalSecretLeaseResolution
from awf.profiles.compose import (
    agent_environment_with_declared_secret_leases,
    agent_environment_with_host_auth,
    profile_agent_environment,
    profile_services,
)
from awf.profiles.models import WorkspaceProfile


@dataclass(frozen=True)
class WorkspaceStackLaunchRequest:
    """Inputs required to launch a workspace's outer Compose stack."""

    workspace_id: str
    layout: WorktreeLayout
    profile: WorkspaceProfile
    companions: tuple[MaterializedCompanionService, ...] = ()
    companion_graph_prevalidated: bool = False


class WorkspaceStackLauncher(Protocol):
    """Small seam for provisioning tests to avoid requiring Docker."""

    async def launch(self, request: WorkspaceStackLaunchRequest) -> ComposeProjectPaths | None:
        """Launch the workspace stack and return rendered compose paths when used."""
        ...


class WorkspaceSecretLeaseResolver(Protocol):
    """Resolves profile-declared local secret leases for the agent container."""

    def resolve(
        self,
        profile: WorkspaceProfile,
        *,
        workspace_id: str,
    ) -> LocalSecretLeaseResolution:
        """Resolve local secret lease mounts and environment for one workspace."""
        ...


class WorkspaceServiceExecutionError(Exception):
    """Raised when profile-declared workspace services fail to start."""

    pass


class ComposeStackLauncher:
    """Render and start the profile-driven outer workspace Compose stack."""

    def __init__(
        self,
        *,
        compose: ComposeManager,
        agent_runtime_image: str,
        auth_mount_resolver: WorkspaceAuthMountResolver | None = None,
        secret_lease_resolver: WorkspaceSecretLeaseResolver | None = None,
        companion_image_builder: CompanionImageBuilder | None = None,
    ) -> None:
        """Wire stack launch dependencies and optional credential resolvers."""
        self._compose = compose
        self._agent_runtime_image = agent_runtime_image
        self._auth_mount_resolver = auth_mount_resolver
        self._secret_lease_resolver = secret_lease_resolver
        self._companion_image_builder = companion_image_builder

    async def launch(self, request: WorkspaceStackLaunchRequest) -> ComposeProjectPaths | None:
        """Render and start the profile stack, including companions and secret metadata."""
        layout = request.layout
        profile = request.profile
        egress_plan = local_egress_plan(profile.security.egress)
        # Linked git worktrees store writable refs/objects in the common mirror.
        # Agents need that metadata writable when they make local commits.
        mirror_mount = AuthMount(
            source=str(layout.mirror_path),
            target=str(layout.mirror_path),
            mode="rw",
        )
        auth_mounts = [mirror_mount]
        secret_lease_resolution: LocalSecretLeaseResolution | None = None
        if self._secret_lease_resolver is not None:
            secret_lease_resolution = await asyncio.to_thread(
                self._secret_lease_resolver.resolve,
                profile,
                workspace_id=request.workspace_id,
            )
            auth_mounts.extend(secret_lease_resolution.mounts)

        satisfied_targets = (
            secret_lease_resolution.satisfied_legacy_targets
            if secret_lease_resolution is not None
            else frozenset()
        )
        satisfied_providers = (
            secret_lease_resolution.satisfied_legacy_providers
            if secret_lease_resolution is not None
            else frozenset()
        )
        suppressed_legacy_targets = satisfied_targets | legacy_provider_targets(satisfied_providers)
        if self._auth_mount_resolver is not None:
            legacy_mounts = await asyncio.to_thread(
                self._auth_mount_resolver.resolve,
                workspace_id=request.workspace_id,
                suppressed_targets=satisfied_targets,
                suppressed_providers=satisfied_providers,
            )
            auth_mounts.extend(
                mount for mount in legacy_mounts if mount.target not in suppressed_legacy_targets
            )
        services = await asyncio.to_thread(
            profile_services,
            profile,
            base_path=layout.worktree_path,
        )
        companion_graph_already_validated = (
            bool(request.companions) and request.companion_graph_prevalidated
        )
        if not companion_graph_already_validated:
            await asyncio.to_thread(
                validate_companion_service_graph,
                profile_services=services,
                companions=request.companions,
                docker_mode=profile.docker.mode,
            )
        # Resolve the effective compose-up budget before pre-building companions so
        # the cache pre-build shares the same subprocess cap the inline `docker
        # compose up` build uses (see _build_companion_services).
        compose_up_timeout_seconds = effective_compose_up_timeout_seconds(
            profile=profile,
            companions=request.companions,
        )
        companions = await self._build_companion_services(
            request.companions,
            capture_timeout_seconds=compose_up_capture_timeout_seconds(
                compose_up_timeout_seconds, wait=True
            ),
        )
        companion_secret_metadata = companion_env_secret_stack_metadata(companions)
        agent_environment = profile_agent_environment(profile)
        if secret_lease_resolution is not None:
            agent_environment = agent_environment_with_declared_secret_leases(
                agent_environment,
                secret_lease_resolution.environment,
            )
        agent_environment = agent_environment_with_host_auth(agent_environment)
        spec = WorkspaceComposeSpec(
            workspace_id=request.workspace_id,
            worktree_host_path=layout.worktree_path,
            agent_runtime_image=self._agent_runtime_image,
            agent_environment=agent_environment,
            docker_mode=profile.docker.mode.value,
            dind_image=profile.docker.dind_image,
            services=services,
            companions=companions,
            auth_mounts=tuple(auth_mounts),
            git_name=DEFAULT_GIT_AUTHOR_NAME,
            git_email=DEFAULT_GIT_AUTHOR_EMAIL,
            network_internal=egress_plan.network_internal,
            host_gateway_enabled=egress_plan.host_gateway_enabled,
            compose_up_timeout_seconds=compose_up_timeout_seconds,
        )
        spec = await self._revalidate_prebuilt_companion_images(spec)
        try:
            paths = await self._compose.up(spec, wait=True)
        except ComposeOperationError as e:
            if e.reason_code == "DOCKER_UNAVAILABLE":
                required_services = [s.name for s in spec.services if s.required]
                if spec.docker_mode == "dind":
                    required_services.append("docker")
                msg = "DOCKER_UNAVAILABLE: Cannot start workspace agent container"
                if required_services:
                    msg = f"{msg} and required services: {required_services}"
                detail = e.stderr.strip() or e.stdout.strip()
                if detail:
                    msg = f"{msg}: {detail}"
                raise WorkspaceServiceExecutionError(msg) from e
            raise
        secret_metadata = _stack_secret_metadata(
            secret_lease_resolution=secret_lease_resolution,
            companion_secret_metadata=companion_secret_metadata,
        )
        if not secret_metadata:
            return paths
        return ComposeProjectPaths(
            project_dir=paths.project_dir,
            compose_file=paths.compose_file,
            secret_lease_mount_metadata=secret_metadata,
        )

    async def _build_companion_services(
        self,
        companions: tuple[MaterializedCompanionService, ...],
        *,
        capture_timeout_seconds: float,
    ) -> tuple[CompanionService, ...]:
        """Render companions, pre-building a cached image per companion when possible.

        Each companion is resolved to a Compose service; when an image builder is
        configured it pre-builds (or reuses) a tagged image so the service can
        reference it via ``image:``. A failed or skipped pre-build leaves the
        service as ``build:`` -- identical to the prior behavior.

        Companions are independent, so their pre-builds are dispatched
        concurrently: a multi-companion workspace's provisioning latency is the
        slowest single build rather than the sum of all builds. The builder
        already collapses same-tag waves to one ``docker build`` (see
        :class:`~awf.node.companion_images.CompanionImageBuilder`), so concurrent
        dispatch is safe; ``gather`` preserves input order, keeping the rendered
        services aligned with ``companions``.

        ``capture_timeout_seconds`` is the effective compose-up subprocess cap used
        by the inline ``docker compose up`` build; passing it to the pre-build keeps
        the cache path's build budget aligned with the configured
        ``compose_up_timeout_seconds`` knob so it never times out earlier than the
        inline build it replaces.
        """

        async def _build_single(companion: MaterializedCompanionService) -> CompanionService:
            service = await asyncio.to_thread(companion_service_from_materialized, companion)
            if self._companion_image_builder is not None:
                tag = await self._companion_image_builder.ensure(
                    name=service.name,
                    commit_sha=companion.commit_sha,
                    build_context=service.build_context,
                    dockerfile=service.dockerfile,
                    relative_build_context=companion.spec.build_context,
                    capture_timeout_seconds=capture_timeout_seconds,
                )
                if tag is not None:
                    service = replace(service, image=tag)
            return service

        services = await asyncio.gather(*(_build_single(companion) for companion in companions))
        return tuple(services)

    async def _revalidate_prebuilt_companion_images(
        self,
        spec: WorkspaceComposeSpec,
    ) -> WorkspaceComposeSpec:
        """Clear vanished pre-built companion images so compose builds inline."""
        if self._companion_image_builder is None:
            return spec
        builder = self._companion_image_builder

        async def _revalidate_single(companion: CompanionService) -> CompanionService:
            if companion.image is None:
                return companion
            if await builder.companion_image_exists(companion.image):
                return companion
            return replace(companion, image=None)

        companions = tuple(
            await asyncio.gather(*(_revalidate_single(companion) for companion in spec.companions))
        )
        if companions == spec.companions:
            return spec
        return replace(spec, companions=companions)


def _stack_secret_metadata(
    *,
    secret_lease_resolution: LocalSecretLeaseResolution | None,
    companion_secret_metadata: dict[str, object],
) -> dict[str, object]:
    """Merge profile secret lease metadata with companion env secret metadata."""
    metadata: dict[str, object] = {}
    if secret_lease_resolution is not None:
        metadata.update(dict(secret_lease_resolution.metadata))
    metadata.update(companion_secret_metadata)
    return metadata


def effective_compose_up_timeout_seconds(
    *,
    profile: WorkspaceProfile,
    companions: tuple[MaterializedCompanionService | WorkspaceCompanionSpec, ...],
) -> int:
    """Return the longest compose-up wait timeout requested for this stack."""
    timeouts = [profile.docker.startup_timeout_seconds]
    timeouts.extend(
        timeout
        for companion in companions
        if (timeout := _companion_compose_up_timeout_seconds(companion)) is not None
    )
    return max(timeouts)


def _companion_compose_up_timeout_seconds(
    companion: MaterializedCompanionService | WorkspaceCompanionSpec,
) -> int | None:
    """Return a companion timeout from either materialized or parsed specs."""
    if isinstance(companion, MaterializedCompanionService):
        return companion.spec.compose_up_timeout_seconds
    return companion.compose_up_timeout_seconds
