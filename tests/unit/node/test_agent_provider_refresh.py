"""Promotion replaces actual Compose auth/env material before agent recreation."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from awf.common.agent_execution import resolve_execution
from awf.control.executor.monitor_handoff import _refresh_selected_agent_material
from awf.node.auth_mounts import ServiceAuthMountResolver
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.models import ProfileRuntime, WorkspaceProfile


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
@pytest.mark.parametrize("switch_agent", [False, True])
@pytest.mark.parametrize(
    "source,target",
    [
        ("native", "ollama"),
        ("native", "openrouter"),
        ("ollama", "native"),
        ("openrouter", "native"),
        ("ollama", "openrouter"),
        ("openrouter", "ollama"),
    ],
)
async def test_refresh_replaces_mounts_and_only_selected_credentials(
    tmp_path, monkeypatch, agent, source, target, switch_agent
):
    target_agent = (
        {"codex": "claude_code", "claude_code": "codex"}[agent] if switch_agent else agent
    )
    home = tmp_path / "declared-home"
    for directory in [".codex", ".claude", ".config/gh", ".ollama"]:
        (home / directory).mkdir(parents=True)
    (home / ".codex/config.toml").write_text('developer_instructions="Keep tests focused"\n')
    (home / ".codex/auth.json").write_text('{"token":"native-secret"}')
    (home / ".claude/settings.json").write_text('{"permissions": {"allow": ["Bash(git *)"]}}')
    profile = WorkspaceProfile(
        name="provider",
        runtime=ProfileRuntime(
            environment={
                "OPENAI_API_KEY": "${NATIVE_CODEX_KEY}",
                "ANTHROPIC_API_KEY": "${NATIVE_CLAUDE_KEY}",
                "OPENROUTER_API_KEY": "${ROUTER_KEY}",
                "TOOL_MODE": "safe",
            }
        ),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "unselected-router-secret")
    monkeypatch.setenv("ROUTER_KEY", "selected-router-secret")
    compose = ComposeManager(
        work_dir=tmp_path / "work",
        template_path=Path(__file__).resolve().parents[3] / "docker/compose/workspace.base.yml.j2",
    )
    launcher = ComposeStackLauncher(
        compose=compose,
        agent_runtime_image="pinned",
        auth_mount_resolver=ServiceAuthMountResolver(host_home=home, work_dir=tmp_path / "auth"),
    )
    layout = WorktreeLayout(
        mirror_path=tmp_path / "mirror.git",
        worktree_path=tmp_path / "checkout",
        branch_name="awf/ws",
    )

    def selection(provider, runtime):
        return resolve_execution(
            agent=runtime,
            provider=None if provider == "native" else provider,
            model="model",
            allow_unverified=True,
            profile=profile.model_dump(),
        )

    initial = WorkspaceStackLaunchRequest(
        workspace_id="ws", layout=layout, profile=profile, execution=selection(source, agent)
    )
    spec, _, _ = await launcher._compose_spec(initial, build_companion_images=False)
    paths = compose.render(spec)
    before = yaml.safe_load(paths.compose_file.read_text())
    assert ("OPENROUTER_API_KEY" in before["services"]["agent"]["environment"]) == (
        source == "openrouter"
    )
    before["services"]["companion"] = {
        "image": "pinned-companion",
        "environment": {"DB_PASSWORD": "keep-existing"},
    }
    before["services"]["agent"]["environment"]["AWF_DATABASE_URL"] = "postgresql://keep-existing"
    paths.compose_file.write_text(yaml.safe_dump(before))
    promoted = WorkspaceStackLaunchRequest(
        workspace_id="ws", layout=layout, profile=profile, execution=selection(target, target_agent)
    )
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
        task_policy={"agent_execution": promoted.execution.model_dump()},
    )
    assert await _refresh_selected_agent_material(
        executor, ws=workspace, profile=profile, compose_file=paths.compose_file
    )
    executor._mark_failed.assert_not_called()
    after = yaml.safe_load(paths.compose_file.read_text())
    assert after["services"]["companion"] == before["services"]["companion"]
    runtime = after["services"]["agent"]
    env = runtime["environment"]
    assert env["AWF_DATABASE_URL"] == "postgresql://keep-existing"
    assert env["TOOL_MODE"] == "safe"
    assert ("OPENROUTER_API_KEY" in env) == (target == "openrouter")
    if target == "openrouter":
        assert env["OPENROUTER_API_KEY"] == "${ROUTER_KEY}"
        assert '"${ROUTER_KEY}"' in paths.compose_file.read_text()
    assert ("OPENAI_API_KEY" in env) == (target == "native" and target_agent == "codex")
    assert ("ANTHROPIC_API_KEY" in env) == (target == "native" and target_agent == "claude_code")
    mounts = runtime["volumes"]
    assert any(":/home/agent/.config/gh:" in m for m in mounts)
    assert any(":/home/agent/.codex:" in m for m in mounts) == (
        target == "native" and target_agent == "codex"
    )
    assert any(":/home/agent/.claude:" in m for m in mounts) == (
        target == "native" and target_agent == "claude_code"
    )
    assert any(":/opt/awf/selected-provider-source.json:" in m for m in mounts) == (
        target != "native"
    )
    assert not any(":/home/agent/.ollama:" in m for m in mounts)
    assert "selected-router-secret" not in paths.compose_file.read_text()
    assert "unselected-router-secret" not in paths.compose_file.read_text()
