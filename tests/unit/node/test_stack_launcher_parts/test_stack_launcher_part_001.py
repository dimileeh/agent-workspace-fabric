"""Stack launcher tests: compose timeouts, docker-missing handling, spec, egress."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from awf.common.git_auth import bitbucket_agent_git_config_entries
from awf.node import stack_launcher as stack_launcher_mod
from awf.node.companion_services import (
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
)
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import WorktreeLayout
from awf.node.secret_mounts import SecretLeaseResolutionError
from awf.node.stack_launcher import (
    ComposeStackLauncher,
    WorkspaceServiceExecutionError,
    WorkspaceStackLaunchRequest,
)
from awf.profiles.compose import (
    hosted_profile_env_passthrough_aliases,
    literal_profile_env_from_compose,
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
    _DeclaredLeaseResolver,
    _DockerUnavailableCompose,
    _FailingDeclaredLeaseResolver,
    _layout,
    _RecordingCompanionImageBuilder,
    _RecordingCompose,
)


@pytest.mark.unit
async def test_compose_stack_launcher_uses_profile_compose_timeout() -> None:
    """The launcher passes the profile compose timeout into the spec."""
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
    """The launcher uses the max profile and companion compose timeout."""
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
    """Docker-unavailable generic launches raise a workspace service error."""
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
    """Docker-unavailable serviceful launches include required service names."""
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
    """Non-docker-unavailable compose errors propagate unchanged."""
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
    """The launcher maps profile settings into a compose spec."""
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
    assert spec.clarification_enabled is True
    assert ("DATABASE_URL", "postgresql://awf@postgres/awf") not in (
        spec.clarification_agent_environment
    )
    assert not any(
        name.startswith(("GIT_", "GH_", "GITHUB_", "BITBUCKET_"))
        for name, _value in spec.clarification_agent_environment
    )
    assert spec.clarification_auth_mounts == ()


@pytest.mark.unit
async def test_compose_stack_launcher_render_builds_metadata_without_compose_up() -> None:
    """Render-only hosted adoption gets stack env metadata without launching Compose."""
    compose = _RecordingCompose()
    lease_resolver = _DeclaredLeaseResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=lease_resolver,
    )
    profile = WorkspaceProfile(
        name="hosted",
        runtime=ProfileRuntime(environment={"OLLAMA_HOST": "http://ollama.profile:11434"}),
        secrets=[
            {
                "name": "openai",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
                "ref": "env/OPENAI_API_KEY",
            }
        ],
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=profile,
        )
    )

    assert paths is not None
    assert paths.compose_file == Path("/tmp/awf-compose/ws_launcher/compose.yml")
    assert paths.secret_lease_mount_metadata["env_count"] == 1
    assert lease_resolver.calls == []
    assert compose.specs == []
    assert compose.waits == []
    assert len(compose.render_specs) == 1
    env = dict(compose.render_specs[0].agent_environment)
    assert env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert hosted_profile_env_passthrough_aliases(
        Path("unused-compose.yml"),
        compose_env=env,
        worker_env={},
    ) == (("OPENAI_API_KEY", "OPENAI_API_KEY"),)


@pytest.mark.unit
async def test_compose_stack_launcher_render_skips_local_secret_resolution() -> None:
    """Hosted render preserves lease targets without resolving Core-local sources."""
    compose = _RecordingCompose()
    lease_resolver = _FailingDeclaredLeaseResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=lease_resolver,
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted",
            "secrets": [
                {
                    "name": "npm",
                    "kind": "env",
                    "target": "NPM_TOKEN",
                    "provider": "env",
                    "ref": "env/NPM_TOKEN",
                },
                {
                    "name": "anthropic",
                    "kind": "env",
                    "target": "ANTHROPIC_API_KEY",
                    "provider": "env",
                    "ref": "env/MY_ANTHROPIC_TOKEN",
                },
                {
                    "name": "npmrc",
                    "kind": "mount",
                    "target": "/home/agent/.npmrc",
                    "provider": "local-file",
                    "ref": "file/.npmrc",
                },
            ],
        }
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=profile,
        )
    )

    assert paths is not None
    assert lease_resolver.calls == []
    assert compose.specs == []
    assert len(compose.render_specs) == 1
    env = dict(compose.render_specs[0].agent_environment)
    assert env["NPM_TOKEN"] != "${NPM_TOKEN}"
    assert env["ANTHROPIC_API_KEY"] != "${MY_ANTHROPIC_TOKEN}"
    assert hosted_profile_env_passthrough_aliases(
        Path("unused-compose.yml"),
        compose_env=env,
        worker_env={},
    ) == (
        ("NPM_TOKEN", "NPM_TOKEN"),
        ("ANTHROPIC_API_KEY", "MY_ANTHROPIC_TOKEN"),
    )
    assert "NPM_TOKEN" not in dict(
        literal_profile_env_from_compose(
            Path("unused-compose.yml"),
            compose_env=env,
            worker_env={},
        )
    )
    assert "ANTHROPIC_API_KEY" not in dict(
        literal_profile_env_from_compose(
            Path("unused-compose.yml"),
            compose_env=env,
            worker_env={},
        )
    )
    assert paths.secret_lease_mount_metadata["env_count"] == 2
    assert "total_env_count" not in paths.secret_lease_mount_metadata
    assert paths.secret_lease_mount_metadata["mount_count"] == 1
    assert paths.secret_lease_mount_metadata["providers"] == ["env", "local-file"]
    assert paths.secret_lease_mount_metadata["targets"] == [
        "NPM_TOKEN",
        "ANTHROPIC_API_KEY",
        "/home/agent/.npmrc",
    ]


@pytest.mark.unit
async def test_compose_stack_launcher_render_maps_provider_env_leases_to_hosted_sources() -> None:
    """Hosted provider leases use real provider source names, not profile refs."""
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=_FailingDeclaredLeaseResolver(),
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted",
            "secrets": [
                {
                    "name": "github",
                    "kind": "env",
                    "target": "GH_TOKEN",
                    "provider": "github",
                    "ref": "token",
                },
                {
                    "name": "bitbucket-token",
                    "kind": "env",
                    "target": "BITBUCKET_API_TOKEN",
                    "provider": "bitbucket",
                    "ref": "token",
                },
                {
                    "name": "bitbucket-email",
                    "kind": "env",
                    "target": "BITBUCKET_EMAIL",
                    "provider": "bitbucket",
                    "ref": "email",
                },
                {
                    "name": "bitbucket-unsupported",
                    "kind": "env",
                    "target": "BB_TOKEN",
                    "provider": "bitbucket",
                    "ref": "token",
                    "required": False,
                },
            ],
        }
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=profile,
        )
    )

    assert paths is not None
    env = dict(compose.render_specs[0].agent_environment)
    assert hosted_profile_env_passthrough_aliases(
        Path("unused-compose.yml"),
        compose_env=env,
        worker_env={},
    ) == (
        ("GH_TOKEN", "AWF_GITHUB_TOKEN"),
        ("GITHUB_TOKEN", "AWF_GITHUB_TOKEN"),
        ("BITBUCKET_API_TOKEN", "BITBUCKET_API_TOKEN"),
        ("BITBUCKET_EMAIL", "BITBUCKET_EMAIL"),
    )
    assert paths.secret_lease_mount_metadata["env_count"] == 4
    assert paths.secret_lease_mount_metadata["providers"] == ["github", "bitbucket"]
    assert paths.secret_lease_mount_metadata["targets"] == [
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "BITBUCKET_API_TOKEN",
        "BITBUCKET_EMAIL",
    ]
    assert paths.secret_lease_mount_metadata["skipped_unresolved_count"] == 1


@pytest.mark.unit
async def test_compose_stack_launcher_render_preserves_hosted_bitbucket_askpass_wiring() -> None:
    """Hosted bitbucket token leases keep the agent git auth wiring local Compose uses."""
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=_FailingDeclaredLeaseResolver(),
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-bitbucket",
            "secrets": [
                {
                    "name": "bitbucket-token",
                    "kind": "env",
                    "target": "BITBUCKET_API_TOKEN",
                    "provider": "bitbucket",
                    "ref": "token",
                },
            ],
        }
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=profile,
        )
    )

    assert paths is not None
    spec = compose.render_specs[0]
    env = dict(spec.agent_environment)
    assert env["BITBUCKET_API_TOKEN"]
    assert env["GIT_ASKPASS"] == "/run/awf/secrets/bb-askpass.sh"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    bitbucket_git_entries = bitbucket_agent_git_config_entries()
    for index, (key, value) in enumerate(bitbucket_git_entries):
        assert env[f"GIT_CONFIG_KEY_{index}"] == key
        assert env[f"GIT_CONFIG_VALUE_{index}"] == value
    assert env["GIT_CONFIG_COUNT"] == str(len(bitbucket_git_entries))

    askpass_mount = next(
        mount for mount in spec.auth_mounts if mount.target == "/run/awf/secrets/bb-askpass.sh"
    )
    assert askpass_mount.source.startswith("/run/awf/hosted-auth-placeholders/")
    assert askpass_mount.mode == "ro"
    assert paths.secret_lease_mount_metadata["env_count"] == 1
    assert (
        paths.secret_lease_mount_metadata["total_env_count"]
        == 1 + 2 + (2 * len(bitbucket_git_entries)) + 1
    )
    assert paths.secret_lease_mount_metadata["total_env_count"] > 1
    assert paths.secret_lease_mount_metadata["mount_count"] == 1


@pytest.mark.unit
async def test_compose_stack_launcher_render_preserves_profile_git_askpass_for_bitbucket() -> None:
    """Hosted bitbucket leases do not override a profile-owned askpass command."""
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=_FailingDeclaredLeaseResolver(),
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-bitbucket-profile-askpass",
            "runtime": {"environment": {"GIT_ASKPASS": "/profile/askpass.sh"}},
            "secrets": [
                {
                    "name": "bitbucket-token",
                    "kind": "env",
                    "target": "BITBUCKET_API_TOKEN",
                    "provider": "bitbucket",
                    "ref": "token",
                },
            ],
        }
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=profile,
        )
    )

    assert paths is not None
    spec = compose.render_specs[0]
    env = dict(spec.agent_environment)
    assert env["GIT_ASKPASS"] == "/profile/askpass.sh"
    assert "GIT_TERMINAL_PROMPT" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert not any(mount.target == "/run/awf/secrets/bb-askpass.sh" for mount in spec.auth_mounts)
    assert paths.secret_lease_mount_metadata["env_count"] == 1
    assert paths.secret_lease_mount_metadata["mount_count"] == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "reason_code"),
    [
        (
            {
                "name": "openai",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
                "ref": "env/1_OPENAI_API_KEY",
            },
            "SECRET_LEASE_SOURCE_INVALID",
        ),
        (
            {
                "name": "openai",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
            },
            "SECRET_LEASE_SOURCE_INVALID",
        ),
        (
            {
                "name": "bitbucket-unsupported",
                "kind": "env",
                "target": "BB_TOKEN",
                "provider": "bitbucket",
                "ref": "token",
            },
            "SECRET_LEASE_TARGET_MISMATCH",
        ),
        (
            {
                "name": "vault",
                "kind": "env",
                "target": "VAULT_TOKEN",
                "provider": "vault",
                "ref": "token",
            },
            "SECRET_LEASE_PROVIDER_UNSUPPORTED",
        ),
        (
            {
                "name": "github-mount",
                "kind": "mount",
                "target": "/home/agent/.config/gh",
                "provider": "github",
                "ref": "token",
            },
            "SECRET_LEASE_TARGET_KIND_MISMATCH",
        ),
        (
            {
                "name": "local-file-env",
                "kind": "env",
                "target": "NPMRC",
                "provider": "local-file",
                "ref": "file/.npmrc",
            },
            "SECRET_LEASE_TARGET_KIND_MISMATCH",
        ),
        (
            {
                "name": "relative-npmrc",
                "kind": "mount",
                "target": "relative-npmrc",
                "provider": "local-file",
                "ref": "file/.npmrc",
            },
            "SECRET_LEASE_TARGET_MISMATCH",
        ),
    ],
)
async def test_compose_stack_launcher_render_fails_required_unrenderable_hosted_secret(
    secret: dict[str, object],
    reason_code: str,
) -> None:
    """Required hosted secret declarations fail before rendering incomplete env."""
    compose = _RecordingCompose()
    lease_resolver = _FailingDeclaredLeaseResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=lease_resolver,
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted",
            "secrets": [secret],
        }
    )

    with pytest.raises(SecretLeaseResolutionError) as raised:
        await launcher.render(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=profile,
            )
        )

    assert raised.value.reason_code == reason_code
    assert raised.value.secret_name == secret["name"]
    assert lease_resolver.calls == []
    assert compose.render_specs == []


@pytest.mark.unit
async def test_compose_stack_launcher_passes_profile_dind_image_to_spec() -> None:
    """The launcher passes the profile DinD image into the compose spec."""
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
    """The launcher preflights profile service dependencies without companions."""
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
    """Default restricted egress uses internal-network compose flags."""
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
    """Explicit open egress keeps public-network compatible flags."""
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
    """Offline egress uses internal-network compose flags."""
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
    """Restricted egress uses internal-network compose flags."""
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
    """Profile service resolution is dispatched through asyncio.to_thread."""
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
            "clarification_enabled": True,
        },
    )
    # No companions in this profile, so no per-companion build runs off-thread.
    assert len(calls) == 2
    assert compose.specs[0].services[0].name == "sidecar"
