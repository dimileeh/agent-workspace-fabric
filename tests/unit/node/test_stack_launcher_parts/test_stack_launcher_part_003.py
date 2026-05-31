"""Stack launcher tests: token placeholders, auth mounts, and declared secret leases."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount
from awf.node.git_manager import WorktreeLayout
from awf.node.secret_mounts import SecretLeaseResolutionError
from awf.node.stack_launcher import (
    ComposeStackLauncher,
    WorkspaceStackLaunchRequest,
)
from awf.profiles.models import WorkspaceProfile
from tests.unit.node.test_stack_launcher_parts._helpers import (
    _DeclaredLeaseResolver,
    _EmptyAuthMountResolver,
    _FailingAuthMountResolver,
    _FailingDeclaredLeaseResolver,
    _layout,
    _ProviderDeclaredLeaseResolver,
    _RecordingAuthMountResolver,
    _RecordingCompose,
    _SuppressibleAuthMountResolver,
)


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
    monkeypatch.setenv("CURSOR_API_KEY", "cursor_secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini_secret")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama_secret")
    monkeypatch.setenv("XAI_API_KEY", "xai_secret")
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
    assert env["CURSOR_API_KEY"] == "${CURSOR_API_KEY}"
    assert env["GEMINI_API_KEY"] == "${GEMINI_API_KEY}"
    assert env["OLLAMA_API_KEY"] == "${OLLAMA_API_KEY}"
    assert env["XAI_API_KEY"] == "${XAI_API_KEY}"
    assert "codex_secret" not in repr(compose.specs[0].agent_environment)
    assert "claude_secret" not in repr(compose.specs[0].agent_environment)
    assert "cursor_secret" not in repr(compose.specs[0].agent_environment)
    assert "gemini_secret" not in repr(compose.specs[0].agent_environment)
    assert "ollama_secret" not in repr(compose.specs[0].agent_environment)
    assert "xai_secret" not in repr(compose.specs[0].agent_environment)


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
