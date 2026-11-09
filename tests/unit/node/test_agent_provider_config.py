"""Exercise real isolated files and launch environments, never host auth."""

import json
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from awf.adapters.provider_config import isolated_provider_config, provider_environment
from awf.common.agent_execution import resolve_execution


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
def test_parallel_workspace_config_and_credentials_are_isolated(tmp_path, agent):
    source = {
        "model": "native",
        "env": {"ANTHROPIC_API_KEY": "native-secret"},
        "model_provider": "native",
        "apiKeyHelper": "steal",
        "agent": "haiku",
        "permissions": {"allow": ["Bash(git *)"]},
        "developer_instructions": "follow AGENTS.md",
        "effortLevel": "high",
    }
    inherited = {
        "OPENAI_API_KEY": "native-secret",
        "ANTHROPIC_API_KEY": "native-secret",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "native",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "OPENROUTER_API_KEY": "selected-secret",
        "GH_TOKEN": "forge-secret",
        "PATH": "/usr/bin",
    }

    def render(model):
        selected = resolve_execution(
            agent=agent, provider="openrouter", model=model, allow_unverified=True
        )
        with isolated_provider_config(
            selected.model_dump(), root=tmp_path, environ=inherited, source_config=source
        ) as (args, env, directory):
            content = (
                directory / ("config.toml" if agent == "codex" else "settings.json")
            ).read_text()
            config = tomllib.loads(content) if agent == "codex" else json.loads(content)
            assert "native-secret" not in content and "selected-secret" not in content
            assert "apiKeyHelper" not in config and "agent" not in config
            assert "effort" not in content.lower()
            assert env.get("OPENAI_API_KEY") is None
            assert env.get("CLAUDE_CODE_USE_BEDROCK") is None
            assert env["GH_TOKEN"] == "forge-secret"
            assert "selected-secret" not in str(args)
            assert (
                config.get("developer_instructions") == "follow AGENTS.md"
                if agent == "codex"
                else config["permissions"] == source["permissions"]
            )
            return directory, env, content

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(render, ["model-a", "model-b"]))
    assert first[0] != second[0]
    assert not first[0].exists() and not second[0].exists()
    assert "model-a" in first[2] and "model-b" in second[2]
    assert source["model"] == "native" and inherited["OPENAI_API_KEY"] == "native-secret"


def test_authless_ollama_and_failed_preparation_cleanup(tmp_path):
    selected = resolve_execution(
        agent="claude_code", provider="ollama", model="qwen3-coder:cloud", allow_unverified=True
    )
    env = provider_environment(
        selected.model_dump(),
        {
            "OLLAMA_API_KEY": "daemon-secret",
            "ANTHROPIC_API_KEY": "native-secret",
            "ANTHROPIC_MODEL": "old",
        },
    )
    assert env["ANTHROPIC_BASE_URL"] == selected.endpoint
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"
    assert "OLLAMA_API_KEY" not in env
    assert "ANTHROPIC_MODEL" not in env
    with (
        pytest.raises(RuntimeError, match="cancelled"),
        isolated_provider_config(
            selected.model_dump(), root=tmp_path, environ={}, source_config={}
        ) as (_, _, directory),
    ):
        assert directory.exists()
        raise RuntimeError("cancelled")
    assert list(tmp_path.iterdir()) == []


def test_missing_selected_credential_fails_without_leaking_values():
    selected = resolve_execution(
        agent="codex", provider="openrouter", model="model", allow_unverified=True
    )
    with pytest.raises(ValueError, match="PROVIDER_CREDENTIAL_MISSING"):
        provider_environment(selected.model_dump(), {"OPENAI_API_KEY": "native-secret"})


def test_safe_codex_tools_preserved_without_provider_env_or_role_settings(tmp_path):
    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    source = {
        "developer_instructions": "Keep tests focused",
        "mcp_servers": {
            "safe": {
                "command": "tool",
                "args": ["serve"],
                "env": {"OPENAI_API_KEY": "native-secret"},
            }
        },
    }
    with isolated_provider_config(
        selected.model_dump(), root=tmp_path, environ={}, source_config=source
    ) as (_, _, directory):
        config = tomllib.loads((directory / "config.toml").read_text())
        assert config["mcp_servers"]["safe"] == {"command": "tool", "args": ["serve"]}
        assert "native-secret" not in str(config)


def test_runtime_redacts_selected_credential_before_any_output(capsys):
    import sys

    from awf.adapters.provider_config import run_cli_redacted

    sentinel = "router-opaque-credential"
    code = "import os,sys; print(os.environ['AWF_SELECTED_PROVIDER_KEY']); sys.stderr.write(os.environ['AWF_SELECTED_PROVIDER_KEY'])"
    result = run_cli_redacted([sys.executable, "-c", code], {"AWF_SELECTED_PROVIDER_KEY": sentinel})
    captured = capsys.readouterr()
    assert result == 0
    assert sentinel not in captured.out + captured.err
    assert "<redacted>" in captured.out and "<redacted>" in captured.err


def test_declared_auth_resolver_mounts_only_sanitized_configuration(tmp_path):
    from awf.node.auth_mounts import ServiceAuthMountResolver

    source = tmp_path / "declared-home" / ".codex"
    source.mkdir(parents=True)
    original = 'model="native"\nmodel_reasoning_effort="xhigh"\ndeveloper_instructions="Keep focused"\n[model_providers.native]\nenv_key="NATIVE_KEY"\n'
    (source / "config.toml").write_text(original)
    (source / "auth.json").write_text('{"token":"native-secret"}')
    resolver = ServiceAuthMountResolver(host_home=source.parent, work_dir=tmp_path / "work")
    mount = resolver.resolve_execution_config(workspace_id="ws", agent="codex")
    content = Path(mount.source).read_text()
    assert json.loads(content) == {"developer_instructions": "Keep focused"}
    assert mount.target == "/opt/awf/selected-provider-source.json" and mount.mode == "ro"
    assert "native-secret" not in content and "NATIVE_KEY" not in content
    assert (source / "config.toml").read_text() == original


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
def test_standalone_runtime_runs_cli_with_selected_config_then_cleans_it(
    tmp_path, monkeypatch, capsys, agent
):
    import sys

    import awf.adapters.provider_config as runtime

    selected = resolve_execution(
        agent=agent, provider="openrouter", model="chosen/model", allow_unverified=True
    )
    executable = tmp_path / "fake-agent"
    executable.write_text(
        "#!/usr/local/bin/python\nimport os,json,pathlib,sys\nroot=pathlib.Path(os.environ.get('CODEX_HOME') or os.environ['CLAUDE_CONFIG_DIR'])\nprint(json.dumps({'config':str(root),'args':sys.argv[1:],'token':os.environ.get('AWF_SELECTED_PROVIDER_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')}))\n"
    )
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "selected-opaque-secret")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "declared-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "declared-claude"))
    monkeypatch.setattr(runtime, "probe_connection", lambda *_args: {"ok": True})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provider_config.py",
            json.dumps(selected.model_dump()),
            str(executable),
            *(["exec"] if agent == "codex" else []),
            "--model",
            "chosen/model",
            "-",
        ],
    )
    assert runtime.main() == 0
    output = capsys.readouterr().out
    observed = json.loads(output)
    assert observed["token"] == "<redacted>"
    config_path = Path(observed["config"])
    assert config_path.parent == Path("/home/agent/.awf/provider-config")
    assert not config_path.exists()
    assert "selected-opaque-secret" not in output
    assert "chosen/model" in observed["args"]


def test_runtime_cancellation_stops_child_and_removes_only_owned_config(tmp_path):
    import os
    import subprocess
    import sys

    import awf.adapters.provider_config as runtime

    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    executable = tmp_path / "cancel-agent"
    executable.write_text(
        "#!/usr/local/bin/python\nimport os,signal,time\nos.kill(os.getppid(),signal.SIGTERM)\ntime.sleep(20)\n"
    )
    executable.chmod(0o755)
    runtime_root = tmp_path / "runtime-owned"
    runtime_root.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("keep")
    script = "import runpy,sys; scope=runpy.run_path(sys.argv[1]); scope['main'].__globals__['probe_connection']=lambda *a:{'ok':True}; sys.argv=['provider_config.py',*sys.argv[2:]]; raise SystemExit(scope['main']())"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            runtime.__file__,
            json.dumps(selected.model_dump()),
            str(executable),
            "exec",
            "-",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "TMPDIR": str(runtime_root),
            "CODEX_HOME": str(tmp_path / "no-native-auth"),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 143, result.stderr
    assert list(runtime_root.iterdir()) == []
    assert unrelated.read_text() == "keep"


@pytest.mark.parametrize("provider", ["openrouter", "ollama"])
async def test_rendered_stack_suppresses_native_mounts_and_competing_profile_env(
    tmp_path, monkeypatch, provider
):
    from awf.node.auth_mounts import ServiceAuthMountResolver
    from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
    from awf.profiles.models import ProfileRuntime, WorkspaceProfile
    from tests.unit.node.test_stack_launcher_parts._helpers import _layout, _RecordingCompose

    home = tmp_path / "declared-home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        'developer_instructions="Keep focused"\nmodel="native"\n'
    )
    (home / ".codex" / "auth.json").write_text('{"token":"native-secret"}')
    (home / ".config" / "gh").mkdir(parents=True)
    selected = resolve_execution(
        agent="codex", provider=provider, model="model", allow_unverified=True
    )
    profile = WorkspaceProfile(
        name="provider",
        runtime=ProfileRuntime(
            environment={
                "OPENAI_API_KEY": "native-secret",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
                "TOOL_MODE": "safe",
            }
        ),
    )
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,
        agent_runtime_image="pinned",
        auth_mount_resolver=ServiceAuthMountResolver(
            host_home=home, work_dir=tmp_path / "auth-work"
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-native-secret")
    await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws", layout=_layout(), profile=profile, execution=selected
        )
    )
    spec = compose.render_specs[0]
    env = dict(spec.agent_environment)
    if provider == "openrouter":
        assert env["OPENROUTER_API_KEY"] == "${OPENROUTER_API_KEY}"
    else:
        assert "OPENROUTER_API_KEY" not in env
    assert env["TOOL_MODE"] == "safe"
    assert "OPENAI_API_KEY" not in env and "CLAUDE_CODE_USE_BEDROCK" not in env
    targets = {mount.target for mount in spec.auth_mounts}
    assert "/home/agent/.codex" not in targets
    assert "/home/agent/.claude" not in targets
    assert "/home/agent/.config/gh" in targets
    safe_mount = next(
        m for m in spec.auth_mounts if m.target == "/opt/awf/selected-provider-source.json"
    )
    assert json.loads(Path(safe_mount.source).read_text()) == {
        "developer_instructions": "Keep focused"
    }
    assert profile.runtime.environment["OPENAI_API_KEY"] == "native-secret"


@pytest.mark.parametrize("provider", ["ollama", "openrouter"])
@pytest.mark.parametrize("source", ["ambient", "profile", "lease", "frozen_alias", "frozen_lease"])
@pytest.mark.parametrize("hosted", [False, True])
async def test_stack_materializes_only_frozen_provider_credentials(
    tmp_path, monkeypatch, provider, source, hosted
):
    import yaml

    from awf.node.compose_manager import ComposeManager
    from awf.node.secret_mounts import LocalSecretLeaseMountResolver
    from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
    from awf.profiles.models import ProfileRuntime, ProfileSecret, WorkspaceProfile
    from tests.unit.node.test_stack_launcher_parts._helpers import _layout, _RecordingCompose

    environment = {"TOOL_MODE": "safe"}
    secrets = [
        ProfileSecret(name="tool", kind="env", provider="env", ref="TOOL_TOKEN", target="GH_TOKEN")
    ]
    if source in {"profile", "frozen_alias"}:
        environment["OPENROUTER_API_KEY"] = "${OTHER_ROUTER_KEY}"
    if source in {"lease", "frozen_lease"}:
        secrets.append(
            ProfileSecret(
                name="router",
                kind="env",
                provider="env",
                ref="env/LEASE_ROUTER_KEY",
                target="OPENROUTER_API_KEY",
            )
        )
    profile = WorkspaceProfile(
        name="selected-provider", runtime=ProfileRuntime(environment=environment), secrets=secrets
    )
    selected_profile = profile.model_dump()
    if source == "frozen_alias":
        selected_profile["runtime"]["environment"]["OPENROUTER_API_KEY"] = "${FROZEN_ROUTER_KEY}"
    if source == "frozen_lease":
        selected_profile["secrets"][-1]["ref"] = "env/FROZEN_ROUTER_KEY"
    selected = resolve_execution(
        agent="codex",
        provider=provider,
        model="coder",
        allow_unverified=True,
        profile=selected_profile,
    )
    for name in (
        "OPENROUTER_API_KEY",
        "OTHER_ROUTER_KEY",
        "LEASE_ROUTER_KEY",
        "FROZEN_ROUTER_KEY",
        "TOOL_TOKEN",
    ):
        monkeypatch.setenv(name, "fake-" + name)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,
        agent_runtime_image="pinned",
        secret_lease_resolver=LocalSecretLeaseMountResolver(host_home=tmp_path, work_dir=tmp_path),
    )
    request = WorkspaceStackLaunchRequest(
        workspace_id="ws", layout=_layout(), profile=profile, execution=selected
    )
    if hosted:
        paths = await launcher.render(request)
        spec = compose.render_specs[0]
    else:
        paths = await launcher.launch(request)
        spec = compose.specs[0]
    assert paths is not None
    env = dict(spec.agent_environment)
    assert "GH_TOKEN" in env and env["TOOL_MODE"] == "safe"
    metadata = paths.secret_lease_mount_metadata
    if provider == "ollama":
        assert "OPENROUTER_API_KEY" not in env
        assert "OPENROUTER_API_KEY" not in metadata.get("targets", [])
    else:
        source_name = selected.credential_ref.removeprefix("env/")
        assert source_name in env["OPENROUTER_API_KEY"]
        if source in {"frozen_alias", "frozen_lease"}:
            assert "OTHER_ROUTER_KEY" not in str(env)
            assert "LEASE_ROUTER_KEY" not in str(env)
    renderer = ComposeManager(
        work_dir=tmp_path,
        template_path=Path("docker/compose/workspace.base.yml.j2"),
    )
    rendered = renderer.render(spec).compose_file
    content = rendered.read_text()
    container = yaml.safe_load(content)["services"]["agent"]
    assert ("OPENROUTER_API_KEY" in container["environment"]) == (provider == "openrouter")
    assert not spec.auth_mounts or all(
        "openrouter" not in mount.target.lower() for mount in spec.auth_mounts
    )
    assert "fake-" not in content
