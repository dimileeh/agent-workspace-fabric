"""Launch per-workspace service stacks from resolved workspace profiles."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Protocol

from awf.common.git_identity import DEFAULT_GIT_AUTHOR_EMAIL, DEFAULT_GIT_AUTHOR_NAME
from awf.node.auth_mounts import WorkspaceAuthMountResolver, legacy_provider_targets
from awf.node.companion_images import CompanionImageBuilder
from awf.node.companion_services import (
    MaterializedCompanionService,
    _hosted_companion_service_from_materialized,
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
from awf.node.stack_launcher_compose_helpers import (
    WorkspaceServiceExecutionError as WorkspaceServiceExecutionError,
)
from awf.node.stack_launcher_compose_helpers import (
    _companion_compose_up_timeout_seconds as _companion_compose_up_timeout_seconds,
)
from awf.node.stack_launcher_compose_helpers import (
    _compose_up_reports_missing_image as _compose_up_reports_missing_image,
)
from awf.node.stack_launcher_compose_helpers import (
    _missing_prebuilt_companion_image_retry_spec,
    _prebuilt_companion_image_count,
    _raise_workspace_service_error_if_docker_unavailable,
)
from awf.node.stack_launcher_compose_helpers import (
    effective_compose_up_timeout_seconds as effective_compose_up_timeout_seconds,
)
from awf.node.stack_launcher_hosted_secret_helpers import (
    _append_hosted_auth_placeholder_mounts,
    _hosted_dynamic_file_auth_mount_targets,
    _hosted_secret_lease_placeholder_resolution,
    _stack_secret_metadata,
)
from awf.profiles.compose import (
    agent_environment_with_declared_secret_leases,
    agent_environment_with_host_auth,
    profile_agent_environment,
    profile_services,
)
from awf.profiles.models import WorkspaceProfile

_HOSTED_LEGACY_FILE_AUTH_MOUNT_TARGETS = (
    "/home/agent/.claude",
    "/home/agent/.claude.json",
    "/home/agent/.codex",
    "/home/agent/.config/gh",
    "/home/agent/.config/opencode",
    "/home/agent/.gitconfig",
    "/home/agent/.grok",
    "/home/agent/.ollama",
    "/home/agent/.ssh",
)


@dataclass(frozen=True)
class WorkspaceStackLaunchRequest:
    """Inputs required to launch a workspace's outer Compose stack."""

    workspace_id: str
    layout: WorktreeLayout
    profile: WorkspaceProfile
    companions: tuple[MaterializedCompanionService, ...] = ()
    companion_graph_prevalidated: bool = False
    on_compose_up_started: Callable[[], Awaitable[None]] | None = None
    """Optional callback invoked when the first compose-up attempt starts."""


class WorkspaceStackLauncher(Protocol):
    """Small seam for provisioning tests to avoid requiring Docker."""

    async def render(self, request: WorkspaceStackLaunchRequest) -> ComposeProjectPaths | None:
        """Render the workspace stack metadata without starting Compose."""
        ...

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
        spec, secret_lease_resolution, companion_secret_metadata = await self._compose_spec(
            request,
            build_companion_images=True,
        )
        try:
            spec = await self._revalidate_prebuilt_companion_images(spec)
        except ComposeOperationError as e:
            _raise_workspace_service_error_if_docker_unavailable(e, spec=spec)
            raise
        missing_prebuilt_retry_budget = _prebuilt_companion_image_count(spec)
        compose_up_started = False

        async def _notify_compose_up_started() -> None:
            nonlocal compose_up_started
            if compose_up_started:
                return
            if request.on_compose_up_started is not None:
                await request.on_compose_up_started()
            compose_up_started = True

        while True:
            try:
                paths = await self._compose.up(
                    spec,
                    wait=True,
                    on_compose_up_started=_notify_compose_up_started,
                )
                break
            except ComposeOperationError as e:
                retry_spec = _missing_prebuilt_companion_image_retry_spec(spec, e)
                if retry_spec is None or missing_prebuilt_retry_budget <= 0:
                    _raise_workspace_service_error_if_docker_unavailable(e, spec=spec)
                    raise
                # Replace spec so retry-time revalidation and any retry failure
                # handling report the compose spec used by the failing attempt.
                spec = retry_spec
                missing_prebuilt_retry_budget -= 1
                try:
                    spec = await self._revalidate_prebuilt_companion_images(spec)
                except ComposeOperationError as retry_error:
                    _raise_workspace_service_error_if_docker_unavailable(
                        retry_error,
                        spec=spec,
                    )
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

    async def render(self, request: WorkspaceStackLaunchRequest) -> ComposeProjectPaths | None:
        """Render stack metadata without starting Compose.

        Hosted PR adoption uses this to preserve the same rendered agent
        environment that local Core derives from the stack while intentionally
        skipping local Compose launch. Profile-declared secret leases are kept
        as secret-free names/targets for hosted runtime resolution; render does
        not resolve Core-local lease sources.
        """
        spec, secret_lease_resolution, companion_secret_metadata = await self._compose_spec(
            request,
            build_companion_images=False,
            use_hosted_secret_placeholders=True,
        )
        paths = self._compose.render(spec)
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

    async def _compose_spec(
        self,
        request: WorkspaceStackLaunchRequest,
        *,
        build_companion_images: bool,
        use_hosted_secret_placeholders: bool = False,
    ) -> tuple[WorkspaceComposeSpec, LocalSecretLeaseResolution | None, dict[str, object]]:
        """Build the rendered stack spec shared by launch and render-only paths."""
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
        if use_hosted_secret_placeholders:
            secret_lease_resolution = _hosted_secret_lease_placeholder_resolution(profile)
            if secret_lease_resolution is not None:
                auth_mounts.extend(secret_lease_resolution.mounts)
        elif self._secret_lease_resolver is not None:
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
        if use_hosted_secret_placeholders and self._auth_mount_resolver is not None:
            _append_hosted_auth_placeholder_mounts(
                auth_mounts,
                _HOSTED_LEGACY_FILE_AUTH_MOUNT_TARGETS,
                suppressed_targets=suppressed_legacy_targets,
            )
        elif self._auth_mount_resolver is not None:
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
        # Give pre-builds and inline builds the same effective compose-up budget.
        compose_up_timeout_seconds = effective_compose_up_timeout_seconds(
            profile=profile,
            companions=request.companions,
        )
        if build_companion_images:
            companions = await self._build_companion_services(
                request.companions,
                capture_timeout_seconds=compose_up_capture_timeout_seconds(
                    compose_up_timeout_seconds, wait=True
                ),
            )
        else:
            companion_service = (
                _hosted_companion_service_from_materialized
                if use_hosted_secret_placeholders
                else companion_service_from_materialized
            )
            companions = tuple(
                await asyncio.gather(
                    *(
                        asyncio.to_thread(companion_service, companion)
                        for companion in request.companions
                    )
                )
            )
        companion_secret_metadata = companion_env_secret_stack_metadata(companions)
        agent_environment = profile_agent_environment(profile)
        if secret_lease_resolution is not None:
            agent_environment = agent_environment_with_declared_secret_leases(
                agent_environment,
                secret_lease_resolution.environment,
            )
        agent_environment = agent_environment_with_host_auth(agent_environment)
        if use_hosted_secret_placeholders and self._auth_mount_resolver is not None:
            _append_hosted_auth_placeholder_mounts(
                auth_mounts,
                _hosted_dynamic_file_auth_mount_targets(agent_environment),
            )
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
        return spec, secret_lease_resolution, companion_secret_metadata

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
            """Resolve and optionally pre-build one materialized companion service."""
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
            """Return a build-backed companion when its pre-built image vanished."""
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
