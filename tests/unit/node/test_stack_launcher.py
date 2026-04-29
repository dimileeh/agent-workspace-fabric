"""Stack launcher tests that avoid a Docker daemon."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount, ComposeProjectPaths, WorkspaceComposeSpec
from awf.node.egress_policy import LocalEgressPolicyError
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


class _EmptyAuthMountResolver:
    def __init__(self) -> None:
        self.workspace_ids: list[str] = []

    def resolve(self, *, workspace_id: str) -> tuple[AuthMount, ...]:
        self.workspace_ids.append(workspace_id)
        return ()


class _FailingAuthMountResolver:
    def __init__(self) -> None:
        self.workspace_ids: list[str] = []

    def resolve(self, *, workspace_id: str) -> tuple[AuthMount, ...]:
        self.workspace_ids.append(workspace_id)
        raise RuntimeError("auth mount resolution failed")


def _layout() -> WorktreeLayout:
    return WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_launcher"),
        branch_name="awf/ws_launcher",
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
    assert ("DATABASE_URL", "postgresql://awf@postgres/awf") in spec.agent_environment
    assert spec.docker_mode == "dind"
    assert spec.git_name == "AWF Agent"
    assert spec.git_email == "awf@example.com"
    assert [service.name for service in spec.services] == ["postgres"]
    assert spec.auth_mounts[0].source == str(layout.mirror_path)
    assert spec.auth_mounts[0].target == str(layout.mirror_path)
    assert spec.auth_mounts[0].mode == "rw"


@pytest.mark.unit
async def test_compose_stack_launcher_default_open_egress_keeps_compatible_flags() -> None:
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
async def test_compose_stack_launcher_mirrored_without_allowlist_uses_internal_network_flags() -> None:
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
                {"name": "mirrored", "security": {"egress": {"mode": "mirrored"}}}
            ),
        )
    )

    assert len(compose.specs) == 1
    assert compose.specs[0].network_internal is True
    assert compose.specs[0].host_gateway_enabled is False


@pytest.mark.unit
async def test_compose_stack_launcher_rejects_allowlist_egress_before_compose_up() -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    with pytest.raises(LocalEgressPolicyError) as raised:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=WorkspaceProfile.model_validate(
                    {
                        "name": "allowlisted",
                        "security": {
                            "egress": {
                                "mode": "allowlist",
                                "allowlist": ["api.github.com"],
                            }
                        },
                    }
                ),
            )
        )

    assert raised.value.reason_code == "LOCAL_EGRESS_ALLOWLIST_UNSUPPORTED"
    assert raised.value.mode == "allowlist"
    assert compose.specs == []
    assert compose.waits == []


@pytest.mark.unit
async def test_compose_stack_launcher_rejects_mirrored_allowlist_before_compose_up() -> None:
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    with pytest.raises(LocalEgressPolicyError) as raised:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=WorkspaceProfile.model_validate(
                    {
                        "name": "mirrored",
                        "security": {
                            "egress": {
                                "mode": "mirrored",
                                "allowlist": ["npm.internal.example"],
                            }
                        },
                    }
                ),
            )
        )

    assert raised.value.reason_code == "LOCAL_EGRESS_MIRRORED_ALLOWLIST_UNSUPPORTED"
    assert raised.value.mode == "mirrored"
    assert compose.specs == []
    assert compose.waits == []


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

    assert calls == [
        (
            stack_launcher_mod.profile_services,
            (profile,),
            {"base_path": layout.worktree_path},
        )
    ]
    assert compose.specs[0].services[0].name == "sidecar"


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
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "${CLAUDE_CODE_OAUTH_TOKEN}"
    assert env["GEMINI_API_KEY"] == "${GEMINI_API_KEY}"
    assert env["OLLAMA_API_KEY"] == "${OLLAMA_API_KEY}"
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

    assert len(calls) == 2
    func, args, kwargs = calls[0]
    assert func == auth_mount_resolver.resolve
    assert args == ()
    assert kwargs == {"workspace_id": "ws_launcher"}
    func, args, kwargs = calls[1]
    assert func == stack_launcher_mod.profile_services
    assert args == (request.profile,)
    assert kwargs == {"base_path": layout.worktree_path}
