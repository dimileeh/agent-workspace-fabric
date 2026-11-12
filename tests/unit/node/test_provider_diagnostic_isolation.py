"""Privileged workers cannot inject forge/native competitors into diagnostics."""

from pathlib import Path

from awf.common.agent_execution import resolve_execution
from awf.node.auth_mounts import ServiceAuthMountResolver
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.models import WorkspaceProfile


async def test_no_ambient_secrets_mounts_or_model_roles(tmp_path, monkeypatch):
    home = tmp_path / "auth"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        'model="competing"\n[mcp_servers.forge]\ncommand="gh"\n'
    )
    (home / ".config/gh").mkdir(parents=True)
    (home / ".ssh").mkdir()
    for key in [
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "OPENROUTER_API_KEY",
    ]:
        monkeypatch.setenv(key, "synthetic-unrelated")
    monkeypatch.setenv("SELECTED", "selected-key")
    selected = resolve_execution(
        agent="codex",
        provider="openrouter",
        model="candidate",
        allow_unverified=True,
        profile={"runtime": {"environment": {"OPENROUTER_API_KEY": "${SELECTED}"}}},
    )
    compose = ComposeManager(
        work_dir=tmp_path / "work",
        template_path=Path("docker/compose/workspace.base.yml.j2").resolve(),
    )
    launcher = ComposeStackLauncher(
        compose=compose,
        agent_runtime_image="pinned",
        auth_mount_resolver=ServiceAuthMountResolver(host_home=home, work_dir=tmp_path / "state"),
    )
    profile = WorkspaceProfile(
        name="diagnostic",
        docker={"mode": "none"},
        runtime={"environment": {"OPENROUTER_API_KEY": "${SELECTED}"}},
    )
    spec, _, _ = await launcher._compose_spec(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_diag",
            layout=WorktreeLayout(
                mirror_path=tmp_path / "mirror",
                worktree_path=tmp_path / "fixture",
                branch_name="fixture",
            ),
            profile=profile,
            execution=selected,
            diagnostic=True,
        ),
        build_companion_images=False,
    )
    assert next(m for m in spec.auth_mounts if m.source == m.target).mode == "ro"
    environment = dict(spec.agent_environment)
    assert environment["OPENROUTER_API_KEY"] == "${SELECTED}"
    assert not any(
        k in environment
        for k in [
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ]
    )
    assert not any(
        m.target
        in {
            "/home/agent/.config/gh",
            "/home/agent/.ssh",
            "/home/agent/.codex",
            "/opt/awf/selected-provider-source.json",
        }
        for m in spec.auth_mounts
    )


def test_native_diagnostic_copies_only_selected_auth_file(tmp_path):
    from awf.node.provider_diagnostics import remove_diagnostic_auth

    home = tmp_path / "host"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token":"synthetic-selected"}')
    (home / ".codex" / "config.toml").write_text('mcp_servers="forge"')
    resolver = ServiceAuthMountResolver(host_home=home, work_dir=tmp_path / "state")
    mounts = resolver.resolve_diagnostic_auth(workspace_id="ws_diag", agent="codex")
    assert len(mounts) == 1
    assert mounts[0].target == "/home/agent/.codex"
    assert mounts[0].mode == "rw"
    assert (Path(mounts[0].source) / "auth.json").read_text() == '{"token":"synthetic-selected"}'
    assert sorted(p.name for p in Path(mounts[0].source).iterdir()) == ["auth.json"]
    (Path(mounts[0].source) / "session-state").write_text("CLI state")
    assert not list((tmp_path / "state").rglob("config.toml"))
    remove_diagnostic_auth(tmp_path / "state", "ws_diag")
    assert not Path(mounts[0].source).exists()
    assert (home / ".codex" / "auth.json").exists()


def test_diagnostic_mount_filter_keeps_credentials_and_tool_leases_only(tmp_path):
    from types import SimpleNamespace

    import pytest

    from awf.node.provider_diagnostics import diagnostic_auth_mounts

    for agent, directory, credential in [
        ("codex", ".codex", "auth.json"),
        ("claude_code", ".claude", ".credentials.json"),
    ]:
        source = tmp_path / directory
        source.mkdir()
        (source / credential).write_text("synthetic-credential")
        mounts = [
            SimpleNamespace(source=str(source), target="/home/agent/" + directory),
            SimpleNamespace(source="lease", target="lease"),
            SimpleNamespace(source="auth", target="/home/agent/" + directory + "/" + credential),
            SimpleNamespace(source="tool", target="/run/tool-secret"),
            SimpleNamespace(source="forge", target="/home/agent/.config/gh"),
            SimpleNamespace(source="config", target="/opt/awf/selected-provider-source.json"),
        ]
        assert diagnostic_auth_mounts(mounts, agent) == mounts[:4]
        (source / "competing-config").write_text("tool configuration")
        with pytest.raises(ValueError, match="PROFILE_UNSUPPORTED"):
            diagnostic_auth_mounts(mounts, agent)


def test_diagnostic_auth_cleanup_refuses_invalid_ids_and_symlinks(tmp_path):
    import pytest

    from awf.node.provider_diagnostics import remove_diagnostic_auth

    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("preserve")
    with pytest.raises(ValueError, match="FIXTURE_FAILED"):
        remove_diagnostic_auth(tmp_path, "../outside")
    root = tmp_path / "auth" / "ws_diag"
    root.parent.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="FIXTURE_FAILED"):
        remove_diagnostic_auth(tmp_path, "ws_diag")
    root.unlink()
    root.mkdir()
    (root / "diagnostic").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="FIXTURE_FAILED"):
        remove_diagnostic_auth(tmp_path, "ws_diag")
    assert marker.read_text() == "preserve"


def test_native_diagnostic_auth_requires_selected_credentials_and_safe_workspace_id(tmp_path):
    import pytest

    home = tmp_path / "host"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model="competing"')
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text('{"mcpServers":{"forge":{}}}')
    work = tmp_path / "state"
    resolver = ServiceAuthMountResolver(host_home=home, work_dir=work)

    for agent in ("codex", "claude_code"):
        assert resolver.resolve_diagnostic_auth(workspace_id="ws_diag", agent=agent) == ()
        with pytest.raises(ValueError, match="^PROVIDER_DIAGNOSTIC_FIXTURE_FAILED$"):
            resolver.resolve_diagnostic_auth(workspace_id="../escape", agent=agent)
    assert not work.exists()
    assert not (tmp_path / "escape").exists()


def test_diagnostic_auth_cleanup_is_idempotent_and_preserves_other_workspace_auth(tmp_path):
    from awf.node.provider_diagnostics import remove_diagnostic_auth

    root = tmp_path / "auth" / "ws_diag"
    diagnostic = root / "diagnostic"
    diagnostic.mkdir(parents=True)
    (diagnostic / "auth.json").write_text("synthetic-selected")
    sibling = root / "selected-provider-source.json"
    sibling.write_text("preserve workspace configuration")

    remove_diagnostic_auth(tmp_path, "ws_diag")
    remove_diagnostic_auth(tmp_path, "ws_diag")
    remove_diagnostic_auth(tmp_path, "ws_absent")

    assert not diagnostic.exists()
    assert sibling.read_text() == "preserve workspace configuration"
