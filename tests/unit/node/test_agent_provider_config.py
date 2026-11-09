"""Exercise real isolated files and launch environments, never host auth."""

import json
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from awf.adapters.provider_config import (
    isolated_provider_config,
    provider_environment,
    safe_provider_config,
)
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
    safe_source = safe_provider_config("codex", source)
    assert safe_source == {
        "developer_instructions": "Keep tests focused",
        "mcp_servers": {"safe": {"command": "tool", "args": ["serve"]}},
    }
    assert source["mcp_servers"]["safe"]["env"] == {"OPENAI_API_KEY": "native-secret"}
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
        f"#!{sys.executable}\nimport os,json,pathlib,sys\nroot=pathlib.Path(os.environ.get('CODEX_HOME') or os.environ['CLAUDE_CONFIG_DIR'])\ncontent=next(root.iterdir()).read_text()\nprint(json.dumps({{'config':str(root),'content':content,'args':sys.argv[1:],'token':os.environ.get('AWF_SELECTED_PROVIDER_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')}}))\n"
    )
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "runtime-home"
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(os_temp))
    monkeypatch.setenv("OPENROUTER_API_KEY", "selected-opaque-secret")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "declared-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "declared-claude"))
    source = tmp_path / ("declared-codex" if agent == "codex" else "declared-claude")
    source.mkdir()
    source_file = source / ("config.toml" if agent == "codex" else "settings.json")
    original = (
        'developer_instructions="Keep focused"\nmodel="native"\n'
        if agent == "codex"
        else '{"language":"English","model":"native","apiKeyHelper":"competing-auth"}'
    )
    source_file.write_text(original)
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
    assert config_path.parent == home / ".awf" / "provider-config"
    assert not config_path.exists()
    assert list(os_temp.iterdir()) == []
    assert "selected-opaque-secret" not in output
    assert "chosen/model" in observed["args"]
    content = observed["content"]
    config = tomllib.loads(content) if agent == "codex" else json.loads(content)
    assert config["model"] == "chosen/model"
    assert "native" not in content and "competing-auth" not in content
    if agent == "codex":
        assert config["developer_instructions"] == "Keep focused"
    else:
        assert config["language"] == "English"
    assert source_file.read_text() == original


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
        f"#!{sys.executable}\nimport os,pathlib,signal,time\npathlib.Path('config.path').write_text(os.environ['CODEX_HOME'])\nos.kill(os.getppid(),signal.SIGTERM)\ntime.sleep(20)\n"
    )
    executable.chmod(0o755)
    home = tmp_path / "runtime-home"
    runtime_root = home / ".awf" / "provider-config"
    runtime_root.mkdir(parents=True)
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
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
            "HOME": str(home),
            "TMPDIR": str(os_temp),
            "CODEX_HOME": str(tmp_path / "no-native-auth"),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 143, result.stderr
    config_path = Path((tmp_path / "config.path").read_text())
    assert config_path.parent == runtime_root
    assert not config_path.exists()
    assert list(runtime_root.iterdir()) == []
    assert list(os_temp.iterdir()) == []
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
                "AWS_REGION": "eu-west-1",
                "AWS_PROFILE": "project-tools",
                "GOOGLE_APPLICATION_CREDENTIALS": "/run/secrets/tool-google.json",
                "GOOGLE_API_KEY": "competing-key",
                "AWS_BEARER_TOKEN_BEDROCK": "competing-key",
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
    assert env["AWS_REGION"] == "eu-west-1"
    assert env["AWS_PROFILE"] == "project-tools"
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/run/secrets/tool-google.json"
    assert "GOOGLE_API_KEY" not in env and "AWS_BEARER_TOKEN_BEDROCK" not in env
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


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
def test_declared_cloud_tool_settings_survive_provider_isolation(agent):
    selected = resolve_execution(
        agent=agent, provider="ollama", model="coder", allow_unverified=True
    )
    tool_env = {
        "AWS_REGION": "eu-west-1",
        "AWS_PROFILE": "project-tools",
        "AWS_ACCESS_KEY_ID": "tool-access",
        "AWS_SECRET_ACCESS_KEY": "tool-secret",
        "GOOGLE_APPLICATION_CREDENTIALS": "/run/secrets/tool-google.json",
        "GOOGLE_CLOUD_PROJECT": "project-tools",
    }
    model_env = {
        "GOOGLE_API_KEY": "competing-key",
        "GOOGLE_MODEL": "competing-model",
        "GOOGLE_GEMINI_BASE_URL": "https://competing.invalid",
        "AWS_BEARER_TOKEN_BEDROCK": "competing-token",
        "AWS_BEDROCK_MODEL": "competing-model",
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": "https://competing.invalid",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": "competing-project",
    }
    env = provider_environment(selected.model_dump(), {**tool_env, **model_env})
    assert all(env.get(k) == v for k, v in tool_env.items())
    assert not set(model_env).intersection(env)


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"version": 999}, "EXECUTION_CONTRACT_UNSUPPORTED"),
        ({"provider": "unsupported"}, "EXECUTION_PROVIDER_UNSUPPORTED"),
        ({"agent": "unsupported"}, "EXECUTION_AGENT_UNSUPPORTED"),
    ],
)
def test_runtime_environment_rejects_unsupported_identity(updates, reason):
    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    with pytest.raises(ValueError, match=f"^{reason}$") as failure:
        provider_environment(
            {**selected.model_dump(), **updates}, {"OPENAI_API_KEY": "unrelated-secret"}
        )
    assert "unrelated-secret" not in str(failure.value)


@pytest.mark.parametrize(
    "arguments,stdout,stderr,returncode",
    [
        (["--contract-version"], "1\n", "", 0),
        ([json.dumps({"version": 999})], "", "EXECUTION_RUNTIME_CONTRACT_UNSUPPORTED\n", 78),
    ],
)
def test_standalone_runtime_negotiates_version_before_configuration(
    monkeypatch, capsys, arguments, stdout, stderr, returncode
):
    import sys

    import awf.adapters.provider_config as runtime

    monkeypatch.setattr(sys, "argv", ["provider_config.py", *arguments])
    assert runtime.main() == returncode
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (stdout, stderr)


@pytest.mark.parametrize("blocked_by", ["project_agents", "probe"])
def test_standalone_runtime_refuses_unsafe_or_unready_execution(
    tmp_path, monkeypatch, capsys, blocked_by
):
    import sys
    from unittest.mock import Mock

    import awf.adapters.provider_config as runtime

    selected = resolve_execution(
        agent="claude_code", provider="ollama", model="coder", allow_unverified=True
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "runtime-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-native-config"))
    monkeypatch.setattr(sys, "argv", ["provider_config.py", json.dumps(selected.model_dump())])
    probe = Mock(return_value={"ok": False, "reason_code": "PROVIDER_MODEL_UNAVAILABLE"})
    monkeypatch.setattr(runtime, "probe_connection", probe)
    if blocked_by == "project_agents":
        (tmp_path / ".claude" / "agents").mkdir(parents=True)
    assert runtime.main() == 78
    captured = capsys.readouterr()
    assert captured.out == ""
    if blocked_by == "project_agents":
        assert captured.err == "EXECUTION_PROJECT_CONFIGURATION_UNSUPPORTED\n"
        probe.assert_not_called()
    else:
        assert captured.err == "PROVIDER_MODEL_UNAVAILABLE\n"
        probe.assert_called_once_with(selected.model_dump(), runtime.os.environ)
    assert not (tmp_path / "runtime-home" / ".awf").exists()


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
def test_standalone_runtime_prefers_sanitized_mount_over_native_configuration(
    tmp_path, monkeypatch, agent
):
    import sys
    from unittest.mock import Mock

    import awf.adapters.provider_config as runtime

    selected = resolve_execution(
        agent=agent, provider="ollama", model="coder", allow_unverified=True
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    native = tmp_path / "native"
    native.mkdir()
    monkeypatch.setenv("CODEX_HOME" if agent == "codex" else "CLAUDE_CONFIG_DIR", str(native))
    source = native / ("config.toml" if agent == "codex" else "settings.json")
    original = (
        'instructions="native instructions"\nmodel="native"\n'
        if agent == "codex"
        else '{"language":"native language","apiKeyHelper":"native-auth"}'
    )
    source.write_text(original)
    safe_source = tmp_path / "sanitized.json"
    safe_config = {"instructions": "mounted instructions", "language": "mounted language"}
    safe_source.write_text(json.dumps(safe_config))
    # Redirect only the image-owned mount; all configuration files are real.
    monkeypatch.setattr(
        runtime,
        "Path",
        Mock(
            wraps=Path,
            side_effect=lambda *parts: (
                safe_source
                if parts == ("/opt/awf/selected-provider-source.json",)
                else Path(*parts)
            ),
        ),
    )
    monkeypatch.setattr(runtime, "probe_connection", Mock(return_value={"ok": True}))
    monkeypatch.setattr(runtime.signal, "signal", Mock())
    directories = []

    def launch(cli, env):
        directory = Path(env["CODEX_HOME" if agent == "codex" else "CLAUDE_CONFIG_DIR"])
        directories.append(directory)
        content = next(directory.iterdir()).read_text()
        config = tomllib.loads(content) if agent == "codex" else json.loads(content)
        key = "instructions" if agent == "codex" else "language"
        assert config[key] == safe_config[key]
        assert config["model"] == "coder"
        assert "native" not in content
        assert cli[-1] == "-"
        return 23

    monkeypatch.setattr(runtime, "run_cli_redacted", launch)
    monkeypatch.setattr(
        sys, "argv", ["provider_config.py", json.dumps(selected.model_dump()), "agent", "exec", "-"]
    )
    assert runtime.main() == 23
    assert len(directories) == 1 and not directories[0].exists()
    assert source.read_text() == original
    assert json.loads(safe_source.read_text()) == safe_config


def test_standalone_runtime_cancellation_removes_isolated_configuration(tmp_path, monkeypatch):
    import signal
    import sys
    from unittest.mock import Mock

    import awf.adapters.provider_config as runtime

    selected = resolve_execution(
        agent="claude_code", provider="ollama", model="coder", allow_unverified=True
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-native-config"))
    monkeypatch.setattr(runtime, "probe_connection", Mock(return_value={"ok": True}))
    register = Mock()
    monkeypatch.setattr(runtime.signal, "signal", register)
    directories = []

    def cancel(_cli, env):
        directory = Path(env["CLAUDE_CONFIG_DIR"])
        directories.append(directory)
        assert (directory / "settings.json").is_file()
        register.assert_called_once()
        signum, handler = register.call_args.args
        assert signum == signal.SIGTERM
        handler(signum, None)

    monkeypatch.setattr(runtime, "run_cli_redacted", cancel)
    monkeypatch.setattr(
        sys, "argv", ["provider_config.py", json.dumps(selected.model_dump()), "agent", "-"]
    )
    with pytest.raises(SystemExit) as failure:
        runtime.main()
    assert failure.value.code == 143
    assert len(directories) == 1 and not directories[0].exists()
    assert list((tmp_path / "home" / ".awf" / "provider-config").iterdir()) == []
