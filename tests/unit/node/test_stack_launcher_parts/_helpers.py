"""Shared fakes for the stack-launcher test parts (no Docker daemon)."""

from __future__ import annotations

from pathlib import Path

from awf.node.compose_manager import (
    AuthMount,
    ComposeOperationError,
    ComposeProjectPaths,
    WorkspaceComposeSpec,
)
from awf.node.git_manager import WorktreeLayout
from awf.node.secret_mounts import LocalSecretLeaseResolution, SecretLeaseResolutionError
from awf.profiles.models import WorkspaceProfile


class _RecordingCompose:
    def __init__(self) -> None:
        self.specs: list[WorkspaceComposeSpec] = []
        self.waits: list[bool] = []

    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        self.specs.append(spec)
        self.waits.append(wait)
        return ComposeProjectPaths(
            project_dir=Path("/tmp/awf-compose/ws_launcher"),
            compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
        )


class _RecordingCompanionImageBuilder:
    def __init__(self) -> None:
        self.capture_timeouts: list[float] = []

    async def ensure(
        self,
        *,
        name: str,
        commit_sha: str,
        build_context: str,
        dockerfile: str,
        relative_build_context: str,
        capture_timeout_seconds: float,
    ) -> str | None:
        del name, commit_sha, build_context, dockerfile, relative_build_context
        self.capture_timeouts.append(capture_timeout_seconds)
        return None  # fall back to build:, leaving the rest of launch() unchanged


class _DockerUnavailableCompose:
    def __init__(self, *, reason_code: str = "DOCKER_UNAVAILABLE") -> None:
        self.reason_code = reason_code
        self.specs: list[WorkspaceComposeSpec] = []

    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        del wait
        self.specs.append(spec)
        raise ComposeOperationError(
            operation="up",
            returncode=1,
            stdout="",
            stderr="docker unavailable",
            reason_code=self.reason_code,
        )


class _RecordingAuthMountResolver:
    def __init__(self) -> None:
        self.workspace_ids: list[str] = []

    def resolve(
        self,
        *,
        workspace_id: str,
        suppressed_targets: frozenset[str] = frozenset(),
        suppressed_providers: frozenset[str] = frozenset(),
    ) -> tuple[AuthMount, ...]:
        del suppressed_targets
        del suppressed_providers
        self.workspace_ids.append(workspace_id)
        return (
            AuthMount(
                source="/host/home/.config/gh",
                target="/home/agent/.config/gh",
                mode="ro",
            ),
            AuthMount(
                source="/host/work/auth/ws_launcher/codex",
                target="/home/agent/.codex",
                mode="rw",
            ),
        )


class _SuppressibleAuthMountResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, frozenset[str], frozenset[str]]] = []

    def resolve(
        self,
        *,
        workspace_id: str,
        suppressed_targets: frozenset[str] = frozenset(),
        suppressed_providers: frozenset[str] = frozenset(),
    ) -> tuple[AuthMount, ...]:
        self.calls.append((workspace_id, suppressed_targets, suppressed_providers))
        mounts = []
        if (
            "/home/agent/.config/gh" not in suppressed_targets
            and "github" not in suppressed_providers
        ):
            mounts.append(
                AuthMount(
                    source="/host/home/.config/gh",
                    target="/home/agent/.config/gh",
                    mode="ro",
                )
            )
        if "/home/agent/.codex" not in suppressed_targets:
            mounts.append(
                AuthMount(
                    source="/host/work/auth/ws_launcher/codex",
                    target="/home/agent/.codex",
                    mode="rw",
                )
            )
        return tuple(mounts)


class _EmptyAuthMountResolver:
    def __init__(self) -> None:
        self.workspace_ids: list[str] = []

    def resolve(
        self,
        *,
        workspace_id: str,
        suppressed_targets: frozenset[str] = frozenset(),
        suppressed_providers: frozenset[str] = frozenset(),
    ) -> tuple[AuthMount, ...]:
        del suppressed_targets
        del suppressed_providers
        self.workspace_ids.append(workspace_id)
        return ()


class _FailingAuthMountResolver:
    def __init__(self) -> None:
        self.workspace_ids: list[str] = []

    def resolve(
        self,
        *,
        workspace_id: str,
        suppressed_targets: frozenset[str] = frozenset(),
        suppressed_providers: frozenset[str] = frozenset(),
    ) -> tuple[AuthMount, ...]:
        del suppressed_targets
        del suppressed_providers
        self.workspace_ids.append(workspace_id)
        raise RuntimeError("auth mount resolution failed")


class _DeclaredLeaseResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def resolve(
        self,
        profile: WorkspaceProfile,
        *,
        workspace_id: str,
    ) -> LocalSecretLeaseResolution:
        self.calls.append((profile.name, workspace_id))
        return LocalSecretLeaseResolution(
            environment=(("OPENAI_API_KEY", "${OPENAI_API_KEY}"),),
            mounts=(
                AuthMount(
                    source="/host/home/.config/gh",
                    target="/home/agent/.config/gh",
                    mode="ro",
                ),
            ),
            metadata={
                "schema": "secret_lease_mount_metadata.v1",
                "mount_plan": "profile_declared_secret_leases",
                "env_count": 1,
                "mount_count": 1,
                "providers": ["env", "local-auth"],
                "targets": ["OPENAI_API_KEY", "/home/agent/.config/gh"],
            },
            satisfied_legacy_targets=frozenset({"/home/agent/.config/gh"}),
            satisfied_legacy_providers=frozenset({"github"}),
        )


class _ProviderDeclaredLeaseResolver:
    def resolve(
        self,
        profile: WorkspaceProfile,
        *,
        workspace_id: str,
    ) -> LocalSecretLeaseResolution:
        del profile, workspace_id
        return LocalSecretLeaseResolution(
            environment=(("GH_TOKEN", "${AWF_GITHUB_TOKEN}"),),
            metadata={
                "schema": "secret_lease_mount_metadata.v1",
                "mount_plan": "profile_declared_secret_leases",
                "env_count": 1,
                "mount_count": 0,
                "providers": ["github"],
                "targets": ["GH_TOKEN"],
            },
            satisfied_legacy_providers=frozenset({"github"}),
        )


class _FailingDeclaredLeaseResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(
        self,
        profile: WorkspaceProfile,
        *,
        workspace_id: str,
    ) -> LocalSecretLeaseResolution:
        del profile
        self.calls.append(workspace_id)
        raise SecretLeaseResolutionError(
            reason_code="SECRET_LEASE_SOURCE_MISSING",
            secret_name="openai",
            provider="env",
            target="OPENAI_API_KEY",
        )


def _layout() -> WorktreeLayout:
    return WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )
