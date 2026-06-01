"""Stack launcher tests: compose timeouts, docker-missing handling, spec, egress."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from awf.node import stack_launcher as stack_launcher_mod
from awf.node.companion_services import (
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
)
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import WorktreeLayout
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
from tests.unit.node.test_stack_launcher_parts._helpers import (
    _DockerUnavailableCompose,
    _layout,
    _RecordingCompanionImageBuilder,
    _RecordingCompose,
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
    """The launcher pre-builds companions using the effective compose timeout budget."""
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
async def test_compose_stack_launcher_maps_revalidation_docker_unavailable(
    tmp_path: Path,
) -> None:
    """Docker-unavailable revalidation failures use workspace service errors."""

    class _RevalidationUnavailableBuilder:
        """Builder double that returns a tag then fails the revalidation probe."""

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
            """Return a pre-built companion image tag for revalidation."""
            del name, commit_sha, build_context, dockerfile, relative_build_context
            del capture_timeout_seconds
            return "awf-companion-backend:abc123def456"

        async def companion_image_exists(self, tag: str) -> bool:
            """Raise the Docker-unavailable probe error under test."""
            del tag
            raise ComposeOperationError(
                operation="image inspect",
                returncode=1,
                stdout="",
                stderr="Cannot connect to the Docker daemon",
                reason_code="DOCKER_UNAVAILABLE",
            )

    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        companion_image_builder=_RevalidationUnavailableBuilder(),  # type: ignore[arg-type]
    )
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
        commit_sha="abc123def456",
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
                companions=(companion,),
                companion_graph_prevalidated=True,
            )
        )

    message = str(raised.value)
    assert "Cannot start workspace agent container" in message
    assert "required services: ['postgres', 'docker']" in message
    assert "Cannot connect to the Docker daemon" in message
    assert compose.specs == []


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
    assert spec.dind_image == "docker:27-dind"
    assert spec.git_name == "AWF Agent"
    assert spec.git_email == "awf@example.com"
    assert [service.name for service in spec.services] == ["postgres"]
    assert spec.auth_mounts[0].source == str(layout.mirror_path)
    assert spec.auth_mounts[0].target == str(layout.mirror_path)
    assert spec.auth_mounts[0].mode == "rw"


@pytest.mark.unit
async def test_compose_stack_launcher_passes_profile_dind_image_to_spec() -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    profile = WorkspaceProfile(
        name="serviceful",
        docker=ProfileDocker(
            mode=DockerMode.dind,
            dind_image="ghcr.io/example/dind:buildx",
        ),
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=profile,
        )
    )

    assert compose.specs[0].dind_image == "ghcr.io/example/dind:buildx"


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
