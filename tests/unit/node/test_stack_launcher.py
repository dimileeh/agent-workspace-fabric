"""Stack launcher tests that avoid a Docker daemon."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from awf.node import stack_launcher as stack_launcher_mod
from awf.node.companion_services import (
    CompanionEnvironmentSecretRef,
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
)
from awf.node.compose_manager import (
    AuthMount,
    ComposeOperationError,
    ComposeProjectPaths,
    WorkspaceComposeSpec,
)
from awf.node.git_manager import WorktreeLayout
from awf.node.secret_mounts import LocalSecretLeaseResolution, SecretLeaseResolutionError
from awf.node.stack_launcher import (
    ComposeStackLauncher,
    WorkspaceServiceExecutionError,
    WorkspaceStackLaunchRequest,
)
from awf.profiles.models import (
    DockerMode,
    ProfileDocker,
    ProfileRuntime,
    ProfileService,
    WorkspaceProfile,
)
from awf.profiles.resolver import ProfileResolutionError


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


@pytest.mark.unit
async def test_compose_stack_launcher_uses_profile_compose_timeout() -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(
                name="serviceful",
                docker=ProfileDocker(startup_timeout_seconds=720),
            ),
        )
    )

    assert compose.specs[0].compose_up_timeout_seconds == 720


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile_timeout", "companion_timeouts", "expected"),
    [
        (300, [900], 900),
        (1200, [900], 1200),
        (300, [600, 1200], 1200),
    ],
)
async def test_compose_stack_launcher_uses_effective_companion_compose_timeout(
    tmp_path: Path,
    profile_timeout: int,
    companion_timeouts: list[int],
    expected: int,
) -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companions: list[MaterializedCompanionService] = []
    for index, timeout in enumerate(companion_timeouts):
        companion_root = tmp_path / f"backend-{index}"
        companion_root.mkdir()
        companions.append(
            MaterializedCompanionService(
                spec=WorkspaceCompanionSpec(
                    name=f"backend{index}",
                    repo_url=f"git@github.com:example/backend-{index}.git",
                    compose_up_timeout_seconds=timeout,
                ),
                layout=WorktreeLayout(
                    mirror_path=tmp_path / f"backend-{index}.git",
                    worktree_path=companion_root,
                    branch_name=f"awf/ws_launcher/companion/backend{index}",
                ),
            )
        )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(
                name="serviceful",
                docker=ProfileDocker(startup_timeout_seconds=profile_timeout),
            ),
            companions=tuple(companions),
            companion_graph_prevalidated=True,
        )
    )

    assert compose.specs[0].compose_up_timeout_seconds == expected


@pytest.mark.unit
async def test_compose_stack_launcher_prebuilds_companion_with_effective_compose_budget(
    tmp_path: Path,
) -> None:
    # Regression for PRRT_kwDOSJAM6s6F504S: the companion image pre-build must be
    # budgeted with the same effective compose-up subprocess cap the inline
    # `docker compose up` uses (2*effective + buffer), not the fixed 1800s default,
    # so caching can never time out a build the inline path would have completed.
    compose = _RecordingCompose()
    builder = _RecordingCompanionImageBuilder()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        companion_image_builder=builder,  # type: ignore[arg-type]
    )
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            compose_up_timeout_seconds=900,
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
        commit_sha="abc123def456",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(
                name="serviceful",
                docker=ProfileDocker(startup_timeout_seconds=300),
            ),
            companions=(companion,),
            companion_graph_prevalidated=True,
        )
    )

    # effective = max(300, 900) = 900; inline up(wait=True) cap = 2*900 + 60.
    assert builder.capture_timeouts == [1860.0]
    assert compose.specs[0].compose_up_timeout_seconds == 900


@pytest.mark.unit
async def test_compose_stack_launcher_fails_when_docker_missing_without_required_services() -> None:
    compose = _DockerUnavailableCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    with pytest.raises(WorkspaceServiceExecutionError) as raised:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=WorkspaceProfile(name="generic"),
            )
        )

    assert "Cannot start workspace agent container" in str(raised.value)
    assert compose.specs[0].services == ()


@pytest.mark.unit
async def test_compose_stack_launcher_reports_required_services_when_docker_missing() -> None:
    compose = _DockerUnavailableCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    profile = WorkspaceProfile(
        name="serviceful",
        docker=ProfileDocker(mode=DockerMode.dind),
        services=[ProfileService(name="postgres", image="postgres:16-alpine")],
    )

    with pytest.raises(WorkspaceServiceExecutionError) as raised:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=profile,
            )
        )

    assert "required services: ['postgres', 'docker']" in str(raised.value)


@pytest.mark.unit
async def test_compose_stack_launcher_reraises_non_docker_unavailable_errors() -> None:
    compose = _DockerUnavailableCompose(reason_code="COMPOSE_COMMAND_FAILED")
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    with pytest.raises(ComposeOperationError) as raised:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=WorkspaceProfile(name="generic"),
            )
        )

    assert raised.value.reason_code == "COMPOSE_COMMAND_FAILED"


@pytest.mark.unit
async def test_compose_stack_launcher_builds_profile_driven_spec() -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    layout = WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )
    profile = WorkspaceProfile(
        name="serviceful",
        runtime=ProfileRuntime(environment={"DATABASE_URL": "postgresql://awf@postgres/awf"}),
        docker=ProfileDocker(mode=DockerMode.dind),
        services=[
            ProfileService(
                name="postgres",
                image="postgres:16-alpine",
                healthcheck_cmd="pg_isready -U awf",
            )
        ],
    )

    paths = await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=profile,
        )
    )

    assert paths.compose_file.name == "compose.yml"
    assert compose.waits == [True]
    assert len(compose.specs) == 1
    spec = compose.specs[0]
    assert spec.workspace_id == "ws_launcher"
    assert spec.worktree_host_path == layout.worktree_path
    assert spec.agent_runtime_image == "custom-agent-runtime:dev"
    assert ("DATABASE_URL", "postgresql://awf@postgres/awf") in spec.agent_environment
    assert spec.docker_mode == "dind"
    assert spec.git_name == "AWF Agent"
    assert spec.git_email == "awf@example.com"
    assert [service.name for service in spec.services] == ["postgres"]
    assert spec.auth_mounts[0].source == str(layout.mirror_path)
    assert spec.auth_mounts[0].target == str(layout.mirror_path)
    assert spec.auth_mounts[0].mode == "rw"


@pytest.mark.unit
async def test_compose_stack_launcher_preflights_profile_dependencies_without_companions() -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    profile = WorkspaceProfile(
        name="serviceful",
        services=[
            ProfileService(name="postgres", image="postgres:16-alpine"),
            ProfileService(
                name="app",
                image="example/app:latest",
                depends_on=["postgres"],
                healthcheck_cmd="curl -fsS http://localhost:8080/health",
            ),
        ],
    )

    with pytest.raises(ProfileResolutionError) as raised:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=profile,
            )
        )

    assert raised.value.reason_code == "COMPANION_SERVICE_DEPENDENCY_UNHEALTHY"
    assert "app->postgres" in str(raised.value)
    assert compose.specs == []


@pytest.mark.unit
async def test_compose_stack_launcher_default_restricted_egress_uses_internal_flags() -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="generic"),
        )
    )

    assert compose.waits == [True]
    assert len(compose.specs) == 1
    assert compose.specs[0].network_internal is True
    assert compose.specs[0].host_gateway_enabled is False


@pytest.mark.unit
async def test_compose_stack_launcher_explicit_open_egress_keeps_compatible_flags() -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile.model_validate(
                {"name": "open", "security": {"egress": {"mode": "open"}}}
            ),
        )
    )

    assert compose.waits == [True]
    assert len(compose.specs) == 1
    assert compose.specs[0].network_internal is False
    assert compose.specs[0].host_gateway_enabled is True


@pytest.mark.unit
async def test_compose_stack_launcher_offline_egress_uses_internal_network_flags() -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile.model_validate(
                {"name": "offline", "security": {"egress": {"mode": "offline"}}}
            ),
        )
    )

    assert len(compose.specs) == 1
    assert compose.specs[0].network_internal is True
    assert compose.specs[0].host_gateway_enabled is False


@pytest.mark.unit
async def test_compose_stack_launcher_restricted_egress_uses_internal_network_flags() -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile.model_validate(
                {"name": "restricted", "security": {"egress": {"mode": "restricted"}}}
            ),
        )
    )

    assert len(compose.specs) == 1
    assert compose.specs[0].network_internal is True
    assert compose.specs[0].host_gateway_enabled is False


@pytest.mark.unit
async def test_compose_stack_launcher_resolves_profile_services_in_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Callable[..., object], tuple[object, ...], dict[str, object]]] = []

    async def fake_to_thread(
        func: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(stack_launcher_mod.asyncio, "to_thread", fake_to_thread)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    layout = WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )
    profile = WorkspaceProfile(
        name="serviceful",
        services=[
            ProfileService(
                name="sidecar",
                build_context="services/sidecar",
            )
        ],
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=profile,
        )
    )

    assert calls[0] == (
        stack_launcher_mod.profile_services,
        (profile,),
        {"base_path": layout.worktree_path},
    )
    assert calls[1] == (
        stack_launcher_mod.validate_companion_service_graph,
        (),
        {
            "profile_services": compose.specs[0].services,
            "companions": (),
            "docker_mode": profile.docker.mode,
        },
    )
    # No companions in this profile, so no per-companion build runs off-thread.
    assert len(calls) == 2
    assert compose.specs[0].services[0].name == "sidecar"


@pytest.mark.unit
async def test_compose_stack_launcher_passes_materialized_companions_to_compose(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    (companion_root / "services" / "api").mkdir(parents=True)
    (companion_root / "config").mkdir()
    (companion_root / "fixtures").mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    layout = _layout()
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            base_branch="development",
            build_context="services/api",
            dockerfile="docker/Dockerfile",
            env_file="config/dev.env",
            environment=(("APP_ENV", "test"),),
            depends_on=("docker",),
            healthcheck_cmd="curl -fsS http://localhost:8000/health",
            ports=((8000, 18000),),
            command="python -m backend",
            volumes=(("./fixtures", "/fixtures"),),
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(
                name="serviceful",
                docker=ProfileDocker(mode=DockerMode.dind),
            ),
            companions=(companion,),
        )
    )

    rendered = compose.specs[0].companions[0]
    assert rendered.name == "backend"
    assert rendered.build_context == str(companion_root / "services" / "api")
    assert rendered.dockerfile == "../../docker/Dockerfile"
    assert rendered.env_file == str(companion_root / "config" / "dev.env")
    assert rendered.depends_on == ("docker",)
    assert rendered.healthcheck_cmd == "curl -fsS http://localhost:8000/health"
    assert rendered.ports == ((8000, 18000),)
    assert rendered.command == "python -m backend"
    assert rendered.volumes == ((str(companion_root / "fixtures"), "/fixtures"),)


@pytest.mark.unit
async def test_compose_stack_launcher_resolves_companion_environment_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "raw-secret-value")
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            base_branch="development",
            environment_secrets=(
                CompanionEnvironmentSecretRef(
                    target="AIRA_API_KEY",
                    provider="env",
                    kind="env",
                    value_from="ANTHROPIC_API_KEY",
                ),
            ),
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
    )

    paths = await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(
                name="serviceful",
                docker=ProfileDocker(mode=DockerMode.dind),
            ),
            companions=(companion,),
        )
    )

    rendered = compose.specs[0].companions[0]
    assert rendered.environment == (
        (
            "AIRA_API_KEY",
            "${ANTHROPIC_API_KEY:?COMPANION_ENV_SECRET_SOURCE_MISSING_OR_"
            "COMPANION_ENV_SECRET_SOURCE_EMPTY: "
            "companion=backend, target=AIRA_API_KEY, provider=env, "
            "source=ANTHROPIC_API_KEY}",
        ),
    )
    assert "raw-secret-value" not in repr(rendered)
    assert paths.secret_lease_mount_metadata["companion_env_secret_count"] == 1
    assert paths.secret_lease_mount_metadata["companion_env_secrets"] == (
        {
            "companion": "backend",
            "target": "AIRA_API_KEY",
            "provider": "env",
            "source": "ANTHROPIC_API_KEY",
            "required": True,
        },
    )


@pytest.mark.unit
async def test_compose_stack_launcher_omits_optional_missing_companion_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OPTIONAL_TOKEN_SOURCE", raising=False)
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            base_branch="development",
            environment_secrets=(
                CompanionEnvironmentSecretRef(
                    target="OPTIONAL_TOKEN",
                    provider="env",
                    kind="env",
                    value_from="OPTIONAL_TOKEN_SOURCE",
                    required=False,
                ),
            ),
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
    )

    paths = await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="serviceful"),
            companions=(companion,),
            companion_graph_prevalidated=True,
        )
    )

    assert compose.specs[0].companions[0].environment == ()
    assert paths.secret_lease_mount_metadata["companion_env_secret_count"] == 0
    assert paths.secret_lease_mount_metadata["companion_omitted_optional_env_secret_count"] == 1


@pytest.mark.unit
async def test_compose_stack_launcher_fails_required_missing_companion_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            base_branch="development",
            environment_secrets=(
                CompanionEnvironmentSecretRef(
                    target="AIRA_API_KEY",
                    provider="env",
                    kind="env",
                    value_from="ANTHROPIC_API_KEY",
                ),
            ),
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
    )

    with pytest.raises(ProfileResolutionError) as exc:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=WorkspaceProfile(name="serviceful"),
                companions=(companion,),
                companion_graph_prevalidated=True,
            )
        )

    assert exc.value.reason_code == "COMPANION_ENV_SECRET_SOURCE_MISSING"
    assert compose.specs == []


@pytest.mark.unit
async def test_compose_stack_launcher_skips_companion_graph_validation_when_prevalidated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()

    def fail_validate_companion_service_graph(**_: object) -> None:
        raise AssertionError("prevalidated companion graph should not be validated again")

    monkeypatch.setattr(
        stack_launcher_mod,
        "validate_companion_service_graph",
        fail_validate_companion_service_graph,
    )
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="serviceful"),
            companions=(
                MaterializedCompanionService(
                    spec=WorkspaceCompanionSpec(
                        name="backend",
                        repo_url="git@github.com:example/backend.git",
                        base_branch="development",
                    ),
                    layout=WorktreeLayout(
                        mirror_path=tmp_path / "backend.git",
                        worktree_path=companion_root,
                        branch_name="awf/ws_launcher/companion/backend",
                    ),
                ),
            ),
            companion_graph_prevalidated=True,
        )
    )

    assert compose.specs[0].companions[0].name == "backend"


@pytest.mark.unit
async def test_compose_stack_launcher_rejects_companion_profile_service_collision(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    with pytest.raises(ProfileResolutionError) as raised:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=WorkspaceProfile(
                    name="serviceful",
                    services=[ProfileService(name="backend", image="backend:latest")],
                ),
                companions=(
                    MaterializedCompanionService(
                        spec=WorkspaceCompanionSpec(
                            name="backend",
                            repo_url="git@github.com:example/backend.git",
                            base_branch="development",
                        ),
                        layout=WorktreeLayout(
                            mirror_path=tmp_path / "backend.git",
                            worktree_path=companion_root,
                            branch_name="awf/ws_launcher/companion/backend",
                        ),
                    ),
                ),
            )
        )

    assert raised.value.reason_code == "COMPANION_SERVICE_NAME_COLLISION"


@pytest.mark.unit
async def test_compose_stack_launcher_passes_github_token_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_raw_secret")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    layout = WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(name="generic"),
        )
    )

    env = dict(compose.specs[0].agent_environment)
    assert env["GH_TOKEN"] == "${AWF_GITHUB_TOKEN}"
    assert env["GITHUB_TOKEN"] == "${AWF_GITHUB_TOKEN}"
    assert "ghp_raw_secret" not in repr(compose.specs[0].agent_environment)


@pytest.mark.unit
async def test_compose_stack_launcher_accepts_standard_gh_token_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWF_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp_raw_secret")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    layout = WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(name="generic"),
        )
    )

    env = dict(compose.specs[0].agent_environment)
    assert env["GH_TOKEN"] == "${GH_TOKEN}"
    assert env["GITHUB_TOKEN"] == "${GH_TOKEN}"
    assert "ghp_raw_secret" not in repr(compose.specs[0].agent_environment)


@pytest.mark.unit
async def test_compose_stack_launcher_passes_provider_auth_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "codex_secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude_secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini_secret")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama_secret")
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    layout = WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(name="generic"),
        )
    )

    env = dict(compose.specs[0].agent_environment)
    assert env["OPENAI_API_KEY"] == "${OPENAI_API_KEY}"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "${CLAUDE_CODE_OAUTH_TOKEN}"
    assert env["GEMINI_API_KEY"] == "${GEMINI_API_KEY}"
    assert env["OLLAMA_API_KEY"] == "${OLLAMA_API_KEY}"
    assert "codex_secret" not in repr(compose.specs[0].agent_environment)
    assert "claude_secret" not in repr(compose.specs[0].agent_environment)
    assert "gemini_secret" not in repr(compose.specs[0].agent_environment)
    assert "ollama_secret" not in repr(compose.specs[0].agent_environment)


@pytest.mark.unit
async def test_compose_stack_launcher_omits_github_token_placeholders_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWF_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    layout = WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(name="generic"),
        )
    )

    env = dict(compose.specs[0].agent_environment)
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


@pytest.mark.unit
async def test_compose_stack_launcher_appends_service_auth_mounts() -> None:
    compose = _RecordingCompose()
    auth_mount_resolver = _RecordingAuthMountResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        auth_mount_resolver=auth_mount_resolver,
    )
    layout = WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(name="generic"),
        )
    )

    assert auth_mount_resolver.workspace_ids == ["ws_launcher"]
    spec = compose.specs[0]
    assert spec.auth_mounts == (
        AuthMount(source=str(layout.mirror_path), target=str(layout.mirror_path), mode="rw"),
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


@pytest.mark.unit
async def test_compose_stack_launcher_uses_declared_leases_before_legacy_auth_mounts() -> None:
    compose = _RecordingCompose()
    declared_resolver = _DeclaredLeaseResolver()
    auth_mount_resolver = _RecordingAuthMountResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=declared_resolver,
        auth_mount_resolver=auth_mount_resolver,
    )
    layout = _layout()
    profile = WorkspaceProfile.model_validate(
        {
            "name": "declared-leases",
            "secrets": [
                {
                    "name": "openai",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": "env/OPENAI_API_KEY",
                },
                {
                    "name": "github-cli-config",
                    "kind": "mount",
                    "target": "/home/agent/.config/gh",
                    "provider": "local-auth",
                    "ref": ".config/gh",
                },
            ],
        }
    )

    paths = await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=profile,
        )
    )

    assert declared_resolver.calls == [("declared-leases", "ws_launcher")]
    assert auth_mount_resolver.workspace_ids == ["ws_launcher"]
    spec = compose.specs[0]
    assert spec.auth_mounts == (
        AuthMount(source=str(layout.mirror_path), target=str(layout.mirror_path), mode="rw"),
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
    assert dict(spec.agent_environment)["OPENAI_API_KEY"] == "${OPENAI_API_KEY}"
    assert "env/OPENAI_API_KEY" not in repr(spec.agent_environment)
    assert paths.secret_lease_mount_metadata["mount_plan"] == "profile_declared_secret_leases"
    assert paths.secret_lease_mount_metadata["mount_count"] == 1


@pytest.mark.unit
async def test_compose_stack_launcher_suppresses_satisfied_legacy_targets_before_resolution() -> (
    None
):
    compose = _RecordingCompose()
    declared_resolver = _DeclaredLeaseResolver()
    auth_mount_resolver = _SuppressibleAuthMountResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=declared_resolver,
        auth_mount_resolver=auth_mount_resolver,
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="declared-leases"),
        )
    )

    assert auth_mount_resolver.calls == [
        ("ws_launcher", frozenset({"/home/agent/.config/gh"}), frozenset({"github"}))
    ]
    assert compose.specs[0].auth_mounts == (
        AuthMount(
            source="/host/awf/git/mirrors/repo.git",
            target="/host/awf/git/mirrors/repo.git",
            mode="rw",
        ),
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


@pytest.mark.unit
async def test_compose_stack_launcher_suppresses_satisfied_legacy_providers_before_resolution() -> (
    None
):
    compose = _RecordingCompose()
    auth_mount_resolver = _SuppressibleAuthMountResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=_ProviderDeclaredLeaseResolver(),
        auth_mount_resolver=auth_mount_resolver,
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="declared-github-provider"),
        )
    )

    assert auth_mount_resolver.calls == [("ws_launcher", frozenset(), frozenset({"github"}))]
    assert compose.specs[0].auth_mounts == (
        AuthMount(
            source="/host/awf/git/mirrors/repo.git",
            target="/host/awf/git/mirrors/repo.git",
            mode="rw",
        ),
        AuthMount(
            source="/host/work/auth/ws_launcher/codex",
            target="/home/agent/.codex",
            mode="rw",
        ),
    )
    assert dict(compose.specs[0].agent_environment)["GH_TOKEN"] == "${AWF_GITHUB_TOKEN}"


@pytest.mark.unit
async def test_compose_stack_launcher_filters_satisfied_provider_targets_after_resolution() -> None:
    compose = _RecordingCompose()
    auth_mount_resolver = _RecordingAuthMountResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=_ProviderDeclaredLeaseResolver(),
        auth_mount_resolver=auth_mount_resolver,
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="declared-github-provider"),
        )
    )

    assert compose.specs[0].auth_mounts == (
        AuthMount(
            source="/host/awf/git/mirrors/repo.git",
            target="/host/awf/git/mirrors/repo.git",
            mode="rw",
        ),
        AuthMount(
            source="/host/work/auth/ws_launcher/codex",
            target="/home/agent/.codex",
            mode="rw",
        ),
    )


@pytest.mark.unit
async def test_compose_stack_launcher_fails_secret_lease_resolution_before_compose_up() -> None:
    compose = _RecordingCompose()
    declared_resolver = _FailingDeclaredLeaseResolver()
    auth_mount_resolver = _RecordingAuthMountResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=declared_resolver,
        auth_mount_resolver=auth_mount_resolver,
    )

    with pytest.raises(SecretLeaseResolutionError) as raised:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=WorkspaceProfile(name="generic"),
            )
        )

    assert raised.value.reason_code == "SECRET_LEASE_SOURCE_MISSING"
    assert declared_resolver.calls == ["ws_launcher"]
    assert auth_mount_resolver.workspace_ids == []
    assert compose.specs == []


@pytest.mark.unit
async def test_compose_stack_launcher_keeps_mirror_mount_when_auth_resolver_returns_empty() -> None:
    compose = _RecordingCompose()
    auth_mount_resolver = _EmptyAuthMountResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        auth_mount_resolver=auth_mount_resolver,
    )
    layout = WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(name="generic"),
        )
    )

    assert auth_mount_resolver.workspace_ids == ["ws_launcher"]
    assert compose.specs[0].auth_mounts == (
        AuthMount(source=str(layout.mirror_path), target=str(layout.mirror_path), mode="rw"),
    )


@pytest.mark.unit
async def test_compose_stack_launcher_propagates_auth_resolution_errors() -> None:
    compose = _RecordingCompose()
    auth_mount_resolver = _FailingAuthMountResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        auth_mount_resolver=auth_mount_resolver,
    )
    layout = WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )

    with pytest.raises(RuntimeError, match="auth mount resolution failed"):
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=layout,
                profile=WorkspaceProfile(name="generic"),
            )
        )

    assert auth_mount_resolver.workspace_ids == ["ws_launcher"]
    assert compose.specs == []


@pytest.mark.unit
async def test_compose_stack_launcher_resolves_service_auth_mounts_in_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Callable[..., object], tuple[object, ...], dict[str, object]]] = []

    async def fake_to_thread(
        func: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(stack_launcher_mod.asyncio, "to_thread", fake_to_thread)
    compose = _RecordingCompose()
    auth_mount_resolver = _RecordingAuthMountResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        auth_mount_resolver=auth_mount_resolver,
    )
    layout = WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
    )

    request = WorkspaceStackLaunchRequest(
        workspace_id="ws_launcher",
        layout=layout,
        profile=WorkspaceProfile(name="generic"),
    )
    await launcher.launch(request)

    # No companions in this profile, so no per-companion build runs off-thread.
    assert len(calls) == 3
    func, args, kwargs = calls[0]
    assert func == auth_mount_resolver.resolve
    assert args == ()
    assert kwargs == {
        "workspace_id": "ws_launcher",
        "suppressed_targets": frozenset(),
        "suppressed_providers": frozenset(),
    }
    func, args, kwargs = calls[1]
    assert func == stack_launcher_mod.profile_services
    assert args == (request.profile,)
    assert kwargs == {"base_path": layout.worktree_path}
    func, args, kwargs = calls[2]
    assert func == stack_launcher_mod.validate_companion_service_graph
    assert args == ()
    assert kwargs == {
        "profile_services": (),
        "companions": (),
        "docker_mode": request.profile.docker.mode,
    }
