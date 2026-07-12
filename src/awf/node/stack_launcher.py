"""Launch per-workspace service stacks from resolved workspace profiles."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Sequence
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
from awf.node.secret_mounts import (
    SECRET_LEASE_PROVIDER_UNSUPPORTED,
    SECRET_LEASE_SOURCE_INVALID,
    SECRET_LEASE_TARGET_KIND_MISMATCH,
    SECRET_LEASE_TARGET_MISMATCH,
    LocalSecretLeaseResolution,
    SecretLeaseResolutionError,
)
from awf.profiles.compose import (
    agent_environment_with_declared_secret_leases,
    agent_environment_with_host_auth,
    profile_agent_environment,
    profile_services,
)
from awf.profiles.compose_env import hosted_env_secret_alias_placeholder
from awf.profiles.models import ProfileSecret, WorkspaceProfile

_HOSTED_RENDER_ENV_SECRET_PROVIDERS = frozenset(("env", "github", "bitbucket"))
_HOSTED_RENDER_MOUNT_SECRET_PROVIDERS = frozenset(("local-file", "host-file", "local-auth", "auth"))
_HOSTED_RENDER_SECRET_PROVIDERS = (
    _HOSTED_RENDER_ENV_SECRET_PROVIDERS | _HOSTED_RENDER_MOUNT_SECRET_PROVIDERS
)
_HOSTED_RENDER_ENV_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOSTED_GITHUB_ENV_SOURCE_NAME = "AWF_GITHUB_TOKEN"
_HOSTED_GITHUB_ENV_TARGET_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")
_HOSTED_BITBUCKET_ENV_TARGET_SOURCE_NAMES = {
    "BITBUCKET_API_TOKEN": "BITBUCKET_API_TOKEN",
    "BITBUCKET_EMAIL": "BITBUCKET_EMAIL",
}
_HOSTED_AUTH_PLACEHOLDER_SOURCE_ROOT = "/run/awf/hosted-auth-placeholders"
_HOSTED_LEGACY_FILE_AUTH_MOUNT_TARGETS = (
    "/home/agent/.claude",
    "/home/agent/.claude.json",
    "/home/agent/.codex",
    "/home/agent/.config/gh",
    "/home/agent/.config/gcloud",
    "/home/agent/.config/opencode",
    "/home/agent/.gemini",
    "/home/agent/.gitconfig",
    "/home/agent/.grok",
    "/home/agent/.ollama",
    "/home/agent/.ssh",
)
_GOOGLE_APPLICATION_CREDENTIALS = "GOOGLE_APPLICATION_CREDENTIALS"


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
        # Resolve the effective compose-up budget before pre-building companions so
        # the cache pre-build shares the same subprocess cap the inline `docker
        # compose up` build uses (see _build_companion_services).
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
            companions = tuple(
                await asyncio.gather(
                    *(
                        asyncio.to_thread(companion_service_from_materialized, companion)
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


def _prebuilt_companion_image_count(spec: WorkspaceComposeSpec) -> int:
    """Return the number of companions still pinned to pre-built image tags."""
    return sum(1 for companion in spec.companions if companion.image is not None)


def _raise_workspace_service_error_if_docker_unavailable(
    exc: ComposeOperationError,
    *,
    spec: WorkspaceComposeSpec,
) -> None:
    """Map Docker availability failures to the workspace service error shape."""
    if exc.reason_code != "DOCKER_UNAVAILABLE":
        return
    required_services = [s.name for s in spec.services if s.required]
    if spec.docker_mode == "dind":
        required_services.append("docker")
    msg = "DOCKER_UNAVAILABLE: Cannot start workspace agent container"
    if required_services:
        msg = f"{msg} and required services: {required_services}"
    detail = exc.stderr.strip() or exc.stdout.strip()
    if detail:
        msg = f"{msg}: {detail}"
    raise WorkspaceServiceExecutionError(msg) from exc


def _missing_prebuilt_companion_image_retry_spec(
    spec: WorkspaceComposeSpec,
    exc: ComposeOperationError,
) -> WorkspaceComposeSpec | None:
    """Clear missing pre-built companion images after a compose-up race."""
    missing_images = frozenset(
        companion.image
        for companion in spec.companions
        if companion.image is not None and _compose_up_reports_missing_image(exc, companion.image)
    )
    if not missing_images:
        return None
    companions = tuple(
        replace(companion, image=None) if companion.image in missing_images else companion
        for companion in spec.companions
    )
    return replace(spec, companions=companions)


def _compose_up_reports_missing_image(exc: ComposeOperationError, image: str) -> bool:
    """Return whether Compose reported that a specific local image tag is absent."""
    detail = f"{exc.stderr}\n{exc.stdout}"
    image_ref = _compose_image_ref_regex(image)
    image_ref_before_colon = _compose_image_ref_before_colon_regex(image)
    patterns = (
        rf"no such image:\s*{image_ref}",
        rf"{image_ref_before_colon}\s*:\s*no such image",
        rf"pull access denied for\s+{image_ref}",
        rf"(?:repository\s+)?{image_ref}\s+does not exist",
    )
    return any(re.search(pattern, detail, flags=re.IGNORECASE) for pattern in patterns)


def _compose_image_ref_regex(image: str) -> str:
    """Return a regex fragment matching an exact Compose image reference."""
    image_ref_chars = r"A-Za-z0-9_.:/-"
    escaped_image = re.escape(image)
    return rf"(?<![{image_ref_chars}])['\"]?{escaped_image}['\"]?(?![{image_ref_chars}])"


def _compose_image_ref_before_colon_regex(image: str) -> str:
    """Return an exact image reference fragment followed by a colon separator."""
    image_ref_chars = r"A-Za-z0-9_.:/-"
    escaped_image = re.escape(image)
    return rf"(?<![{image_ref_chars}])['\"]?{escaped_image}['\"]?(?=\s*:)"


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


def _hosted_secret_lease_placeholder_resolution(
    profile: WorkspaceProfile,
) -> LocalSecretLeaseResolution | None:
    """Return secret-free lease names/targets for hosted render-only stacks."""
    if not profile.secrets:
        return None

    env: dict[str, str] = {}
    providers: list[str] = []
    targets: list[str] = []
    mounts: list[AuthMount] = []
    satisfied_legacy_targets: set[str] = set()
    satisfied_legacy_providers: set[str] = set()
    mount_count = 0
    skipped_unresolved_count = 0

    for secret in profile.secrets:
        provider = _hosted_secret_provider(secret.provider)
        if provider is None:
            skipped_unresolved_count += 1
            continue
        if provider not in _HOSTED_RENDER_SECRET_PROVIDERS:
            skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                secret,
                provider=provider,
                reason_code=SECRET_LEASE_PROVIDER_UNSUPPORTED,
            )
            continue
        if provider in _HOSTED_RENDER_ENV_SECRET_PROVIDERS:
            if secret.kind != "env":
                skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                    secret,
                    provider=provider,
                    reason_code=SECRET_LEASE_TARGET_KIND_MISMATCH,
                )
                continue
            pairs = _hosted_env_secret_alias_pairs(secret, provider=provider)
            if pairs is None:
                skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                    secret,
                    provider=provider,
                    reason_code=_hosted_env_secret_unrenderable_reason(provider),
                )
                continue
            for target, source_name in pairs:
                if target not in env:
                    env[target] = hosted_env_secret_alias_placeholder(source_name)
                _append_unique_hosted_secret_value(targets, target)
            _append_unique_hosted_secret_value(providers, provider)
            if provider == "github":
                satisfied_legacy_providers.add(provider)
            continue
        if secret.kind != "mount":
            skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                secret,
                provider=provider,
                reason_code=SECRET_LEASE_TARGET_KIND_MISMATCH,
            )
            continue
        if not secret.target.startswith("/"):
            skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                secret,
                provider=provider,
                reason_code=SECRET_LEASE_TARGET_MISMATCH,
            )
            continue
        if provider in _HOSTED_RENDER_MOUNT_SECRET_PROVIDERS:
            mount_count += 1
            _append_unique_hosted_secret_value(providers, provider)
            _append_unique_hosted_secret_value(targets, secret.target)
            _append_hosted_auth_placeholder_mounts(mounts, (secret.target,))
            satisfied_legacy_targets.add(secret.target)
            continue

    if not env and mount_count == 0 and not skipped_unresolved_count:
        return None

    metadata: dict[str, object] = {
        "schema": "secret_lease_mount_metadata.v1",
        "mount_plan": "profile_declared_secret_leases",
        "env_count": len(env),
        "mount_count": mount_count,
        "providers": providers,
        "targets": targets,
    }
    if skipped_unresolved_count:
        metadata["skipped_unresolved_count"] = skipped_unresolved_count
    return LocalSecretLeaseResolution(
        environment=tuple(env.items()),
        mounts=tuple(mounts),
        metadata=metadata,
        satisfied_legacy_targets=frozenset(satisfied_legacy_targets),
        satisfied_legacy_providers=frozenset(satisfied_legacy_providers),
    )


def _skip_or_raise_unrenderable_hosted_secret(
    secret: ProfileSecret,
    *,
    provider: str,
    reason_code: str,
) -> int:
    if secret.required:
        raise SecretLeaseResolutionError(
            reason_code=reason_code,
            secret_name=secret.name,
            provider=provider,
            target=secret.target,
            kind=secret.kind,
        )
    return 1


def _hosted_env_secret_unrenderable_reason(provider: str) -> str:
    if provider == "env":
        return SECRET_LEASE_SOURCE_INVALID
    return SECRET_LEASE_TARGET_MISMATCH


def _hosted_secret_provider(provider: str | None) -> str | None:
    if provider is None:
        return None
    normalized = provider.strip().lower()
    return normalized or None


def _hosted_env_secret_alias_pairs(
    secret: ProfileSecret,
    *,
    provider: str,
) -> tuple[tuple[str, str], ...] | None:
    """Return hosted target/source aliases matching local provider lease rules."""
    if provider == "env":
        source_name = _hosted_env_secret_source_name(secret.ref, fallback=secret.target)
        if source_name is None:
            return None
        return ((secret.target, source_name),)
    if provider == "github":
        if secret.target not in _HOSTED_GITHUB_ENV_TARGET_NAMES:
            return None
        return tuple(
            (target, _HOSTED_GITHUB_ENV_SOURCE_NAME) for target in _HOSTED_GITHUB_ENV_TARGET_NAMES
        )
    if provider == "bitbucket":
        source_name = _HOSTED_BITBUCKET_ENV_TARGET_SOURCE_NAMES.get(secret.target)
        if source_name is None:
            return None
        return ((secret.target, source_name),)
    return None


def _hosted_env_secret_source_name(ref: str | None, *, fallback: str) -> str | None:
    raw = (ref or "").strip()
    if raw.startswith("env/"):
        raw = raw[len("env/") :]
    candidate = raw or fallback
    if not _HOSTED_RENDER_ENV_REF_RE.fullmatch(candidate):
        return None
    return candidate


def _append_unique_hosted_secret_value(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _append_hosted_auth_placeholder_mounts(
    mounts: list[AuthMount],
    targets: Sequence[str],
    *,
    suppressed_targets: frozenset[str] = frozenset(),
) -> None:
    seen = {mount.target for mount in mounts}
    for target in targets:
        if target in seen or target in suppressed_targets or not target.startswith("/"):
            continue
        mounts.append(
            AuthMount(
                source=_hosted_auth_placeholder_source(target),
                target=target,
                mode="ro",
            )
        )
        seen.add(target)


def _hosted_auth_placeholder_source(target: str) -> str:
    name = target.strip("/").replace("/", "__") or "root"
    return f"{_HOSTED_AUTH_PLACEHOLDER_SOURCE_ROOT}/{name}"


def _hosted_dynamic_file_auth_mount_targets(
    agent_environment: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    google_credentials_target = _hosted_google_application_credentials_target(agent_environment)
    if google_credentials_target is None:
        return ()
    return (google_credentials_target,)


def _hosted_google_application_credentials_target(
    agent_environment: tuple[tuple[str, str], ...],
) -> str | None:
    raw = dict(agent_environment).get(_GOOGLE_APPLICATION_CREDENTIALS)
    if raw is None:
        return None
    if raw in (
        f"${{{_GOOGLE_APPLICATION_CREDENTIALS}}}",
        f"${_GOOGLE_APPLICATION_CREDENTIALS}",
    ):
        target = os.environ.get(_GOOGLE_APPLICATION_CREDENTIALS, "")
    elif "$" in raw:
        return None
    else:
        target = raw
    if not target.startswith("/"):
        return None
    return target


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
