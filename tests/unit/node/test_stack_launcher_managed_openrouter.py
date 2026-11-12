"""Managed OpenRouter stacks carry no static key and never the management key."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from awf.common.agent_execution import resolve_execution
from awf.control.executor.monitor_handoff import _refresh_selected_agent_material
from awf.node.auth_mounts import ServiceAuthMountResolver
from awf.node.companion_services import MaterializedCompanionService, WorkspaceCompanionSpec
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import WorktreeLayout
from awf.node.secret_mounts import LocalSecretLeaseMountResolver, SecretLeaseResolutionError
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.models import ProfileRuntime, ProfileSecret, ProfileService, WorkspaceProfile

_STATIC = "sk-or-v1-static-worker-sentinel"
_MANAGEMENT = "sk-or-mgmt-stack-sentinel"


def _managed(agent: str = "codex"):
    return resolve_execution(
        agent=agent,
        provider="openrouter",
        model="moonshotai/kimi-k2.7-code",
        allow_unverified=True,
        managed_openrouter=True,
    )


def _launcher(tmp_path: Path) -> tuple[ComposeStackLauncher, ComposeManager, WorktreeLayout]:
    home = tmp_path / "home"
    (home / ".config/gh").mkdir(parents=True)
    compose = ComposeManager(
        work_dir=tmp_path / "work",
        template_path=Path(__file__).resolve().parents[3] / "docker/compose/workspace.base.yml.j2",
    )
    launcher = ComposeStackLauncher(
        compose=compose,
        agent_runtime_image="pinned",
        auth_mount_resolver=ServiceAuthMountResolver(host_home=home, work_dir=tmp_path / "auth"),
        secret_lease_resolver=LocalSecretLeaseMountResolver(
            host_home=home, work_dir=tmp_path / "leases"
        ),
    )
    layout = WorktreeLayout(
        mirror_path=tmp_path / "mirror.git",
        worktree_path=tmp_path / "checkout",
        branch_name="awf/ws",
    )
    return launcher, compose, layout


def _profile(**runtime_env: str) -> WorkspaceProfile:
    return WorkspaceProfile(
        name="managed",
        runtime=ProfileRuntime(
            environment={"OPENROUTER_API_KEY": "${ROUTER_KEY}", "TOOL_MODE": "safe", **runtime_env}
        ),
        secrets=[
            ProfileSecret(
                name="router",
                target="OPENROUTER_API_KEY",
                kind="env",
                provider="env",
                ref="env/ROUTER_KEY",
            )
        ],
    )


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
async def test_managed_stack_renders_no_openrouter_credential(tmp_path, monkeypatch, agent):
    monkeypatch.setenv("OPENROUTER_API_KEY", _STATIC)
    monkeypatch.setenv("ROUTER_KEY", _STATIC)
    launcher, compose, layout = _launcher(tmp_path)
    request = WorkspaceStackLaunchRequest(
        workspace_id="ws", layout=layout, profile=_profile(), execution=_managed(agent)
    )

    spec, _, _ = await launcher._compose_spec(request, build_companion_images=False)
    paths = compose.render(spec)

    environment = dict(spec.agent_environment)
    assert "OPENROUTER_API_KEY" not in environment
    assert environment["TOOL_MODE"] == "safe"
    rendered = paths.compose_file.read_text()
    assert "OPENROUTER_API_KEY" not in rendered
    assert _STATIC not in rendered and "ROUTER_KEY" not in rendered
    agent_service = yaml.safe_load(rendered)["services"]["agent"]
    assert any(
        ":/opt/awf/selected-provider-source.json:" in mount for mount in agent_service["volumes"]
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda: {"profile": _profile(TOOL_TOKEN="${OPENROUTER_MANAGEMENT_KEY}")},
        lambda: {"profile": _profile(TOOL_TOKEN="prefix-$OPENROUTER_MANAGEMENT_KEY")},
        lambda: {
            "profile": _profile().model_copy(
                update={
                    "services": [
                        ProfileService(
                            name="sidecar",
                            image="sidecar:pinned",
                            environment={"KEY": "${OPENROUTER_MANAGEMENT_KEY:-}"},
                        )
                    ]
                }
            )
        },
        lambda: {
            "profile": _profile().model_copy(
                update={
                    "secrets": [
                        ProfileSecret(
                            name="mgmt",
                            target="TOOL_TOKEN",
                            kind="env",
                            provider="env",
                            ref="env/OPENROUTER_MANAGEMENT_KEY",
                        )
                    ]
                }
            )
        },
        lambda: {
            "profile": _profile(),
            "companions": (
                MaterializedCompanionService(
                    spec=WorkspaceCompanionSpec(
                        name="api",
                        repo_url="https://example.com/api.git",
                        environment=(("TOKEN", "${OPENROUTER_MANAGEMENT_KEY}"),),
                    ),
                    layout=WorktreeLayout(
                        mirror_path=Path("/tmp/m.git"),
                        worktree_path=Path("/tmp/api"),
                        branch_name="awf/api",
                    ),
                ),
            ),
        },
    ],
)
@pytest.mark.parametrize("managed", [True, False])
async def test_profiles_referencing_the_management_key_are_rejected(
    tmp_path, monkeypatch, build, managed
):
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT)
    launcher, _compose, layout = _launcher(tmp_path)
    selection = (
        _managed()
        if managed
        else resolve_execution(agent="opencode", model="openrouter/moonshotai/kimi-k2")
    )
    request = WorkspaceStackLaunchRequest(
        workspace_id="ws", layout=layout, execution=selection, **build()
    )

    with pytest.raises(ValueError, match="PROVIDER_MANAGEMENT_CREDENTIAL_FORBIDDEN") as raised:
        await launcher._compose_spec(request, build_companion_images=False)
    assert _MANAGEMENT not in str(raised.value)


def test_env_secret_leases_never_resolve_the_management_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT)
    resolver = LocalSecretLeaseMountResolver(host_home=tmp_path, work_dir=tmp_path / "leases")
    profile = WorkspaceProfile(
        name="hostile",
        secrets=[
            ProfileSecret(
                name="mgmt",
                target="TOOL_TOKEN",
                kind="env",
                provider="env",
                ref="OPENROUTER_MANAGEMENT_KEY",
            )
        ],
    )
    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(profile, workspace_id="ws")
    assert raised.value.reason_code == "PROVIDER_MANAGEMENT_CREDENTIAL_FORBIDDEN"
    assert _MANAGEMENT not in str(raised.value)


async def test_monitor_refresh_of_a_managed_selection_writes_no_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", _STATIC)
    monkeypatch.setenv("ROUTER_KEY", _STATIC)
    launcher, compose, layout = _launcher(tmp_path)
    static = resolve_execution(
        agent="codex",
        provider="openrouter",
        model="moonshotai/kimi-k2.7-code",
        allow_unverified=True,
        profile=_profile().model_dump(),
    )
    initial = WorkspaceStackLaunchRequest(
        workspace_id="ws", layout=layout, profile=_profile(), execution=static
    )
    spec, _, _ = await launcher._compose_spec(initial, build_companion_images=False)
    paths = compose.render(spec)
    assert "OPENROUTER_API_KEY" in paths.compose_file.read_text()
    executor = SimpleNamespace(
        _stack_launcher=launcher,
        _trusted_git_roots_for_workspace=lambda **_: (tmp_path / "admin", layout.mirror_path),
        _config=SimpleNamespace(worktrees_root=layout.worktree_path.parent),
        _mark_failed=AsyncMock(),
    )
    workspace = SimpleNamespace(
        id=layout.worktree_path.name,
        repo_url="https://example.com/repo",
        branch_name=layout.branch_name,
        monitor_claimed_by=None,
        task_policy={"agent_execution": _managed().model_dump(mode="json")},
    )

    assert await _refresh_selected_agent_material(
        executor, ws=workspace, profile=_profile(), compose_file=paths.compose_file
    )

    executor._mark_failed.assert_not_called()
    rendered = paths.compose_file.read_text()
    assert "OPENROUTER_API_KEY" not in rendered and _STATIC not in rendered
    assert "${ROUTER_KEY}" not in rendered


def test_bound_profile_validation_accepts_the_managed_reference():
    from awf.node.provisioner_helpers import _validate_bound_execution_profile
    from awf.profiles.resolver import ProfileResolutionError

    profile = _profile()
    workspace = SimpleNamespace(
        task_policy={
            "agent_execution": _managed().model_dump(mode="json"),
            "provider_budget": {"managed": True, "limit_usd": 10.0},
        },
        resolved_profile=profile.model_dump(mode="json", by_alias=True),
    )
    _validate_bound_execution_profile(workspace, profile)

    hostile = _profile(OPENROUTER_API_KEY="sk-or-v1-inline-value")
    workspace.resolved_profile = hostile.model_dump(mode="json", by_alias=True)
    with pytest.raises(ProfileResolutionError):
        _validate_bound_execution_profile(workspace, hostile)
