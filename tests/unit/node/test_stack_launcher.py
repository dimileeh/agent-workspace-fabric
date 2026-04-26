"""Stack launcher tests that avoid a Docker daemon."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount, ComposeProjectPaths, WorkspaceComposeSpec
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.models import (
    DockerMode,
    ProfileDocker,
    ProfileRuntime,
    ProfileService,
    WorkspaceProfile,
)


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


class _RecordingAuthMountResolver:
    def __init__(self) -> None:
        self.workspace_ids: list[str] = []

    def resolve(self, *, workspace_id: str) -> tuple[AuthMount, ...]:
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
    assert spec.agent_environment == (("DATABASE_URL", "postgresql://awf@postgres/awf"),)
    assert spec.docker_mode == "dind"
    assert spec.git_name == "AWF Agent"
    assert spec.git_email == "awf@example.com"
    assert [service.name for service in spec.services] == ["postgres"]
    assert spec.auth_mounts[0].source == str(layout.mirror_path)
    assert spec.auth_mounts[0].target == str(layout.mirror_path)
    assert spec.auth_mounts[0].mode == "rw"


@pytest.mark.unit
async def test_compose_stack_launcher_passes_github_token_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_raw_secret")
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
async def test_compose_stack_launcher_omits_github_token_placeholders_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWF_GITHUB_TOKEN", raising=False)
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

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(name="generic"),
        )
    )

    assert len(calls) == 1
    func, args, kwargs = calls[0]
    assert func == auth_mount_resolver.resolve
    assert args == ()
    assert kwargs == {"workspace_id": "ws_launcher"}
