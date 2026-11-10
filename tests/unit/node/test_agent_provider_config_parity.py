"""Third-party runner parity: role models, bounded retries, project instructions."""

import json
import tomllib

import pytest

from awf.adapters.provider_config import isolated_provider_config, provider_environment
from awf.common.agent_execution import resolve_execution

_TOOL_ENV = {"GH_TOKEN": "tool-token", "PATH": "/usr/bin", "AWS_REGION": "eu-west-1"}
_COMPETING_ENV = {
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "native",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "native",
    "CLAUDE_CODE_SUBAGENT_MODEL": "native",
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "0",
    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "0",
    "CLAUDE_CODE_EFFORT_LEVEL": "max",
}


@pytest.mark.parametrize("provider", ["ollama", "openrouter"])
def test_claude_third_party_environment_is_exact(provider):
    selected = resolve_execution(
        agent="claude_code", provider=provider, model="chosen/model", allow_unverified=True
    )
    credential = "selected-secret" if provider == "openrouter" else "ollama"
    environ = {**_TOOL_ENV, **_COMPETING_ENV, "OPENROUTER_API_KEY": "selected-secret"}

    env = provider_environment(selected.model_dump(), environ)

    assert env == {
        **_TOOL_ENV,
        "ANTHROPIC_BASE_URL": selected.endpoint,
        "ANTHROPIC_AUTH_TOKEN": credential,
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "chosen/model",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "chosen/model",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "chosen/model",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "chosen/model",
        "CLAUDE_CODE_SUBAGENT_MODEL": "chosen/model",
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
        "CLAUDE_CODE_EFFORT_LEVEL": "auto",
    }


def test_codex_third_party_environment_is_exact():
    selected = resolve_execution(
        agent="codex", provider="openrouter", model="chosen/model", allow_unverified=True
    )
    environ = {**_TOOL_ENV, **_COMPETING_ENV, "OPENROUTER_API_KEY": "selected-secret"}

    env = provider_environment(selected.model_dump(), environ)

    assert env == {**_TOOL_ENV, "AWF_SELECTED_PROVIDER_KEY": "selected-secret"}


def test_claude_isolated_args_load_project_sources_only(tmp_path):
    selected = resolve_execution(
        agent="claude_code", provider="ollama", model="coder", allow_unverified=True
    )
    source = {"language": "English", "env": {"X": "y"}, "apiKeyHelper": "steal"}
    with isolated_provider_config(
        selected.model_dump(), root=tmp_path, environ={}, source_config=source
    ) as (args, _env, directory):
        settings = json.loads((directory / "settings.json").read_text())
    assert args == ["--setting-sources", "project", "--settings", str(directory / "settings.json")]
    # Flag settings outrank project settings env, so project env can neither
    # turn subagent forcing off nor put effort back on.
    assert settings == {
        "language": "English",
        "model": "coder",
        "env": {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", "CLAUDE_CODE_EFFORT_LEVEL": "auto"},
    }


def test_codex_config_restores_bounded_retries_and_excludes_credential_from_shell(tmp_path):
    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    with isolated_provider_config(
        selected.model_dump(), root=tmp_path, environ={}, source_config={}
    ) as (_args, _env, directory):
        config = tomllib.loads((directory / "config.toml").read_text())
    provider = config["model_providers"]["awf_selected"]
    assert (provider["request_max_retries"], provider["stream_max_retries"]) == (4, 5)
    assert config["shell_environment_policy"] == {"exclude": ["AWF_SELECTED_PROVIDER_KEY"]}


def test_standalone_runtime_allows_project_claude_agents(tmp_path, monkeypatch):
    import sys
    from unittest.mock import Mock

    import awf.adapters.provider_config as runtime

    selected = resolve_execution(
        agent="claude_code", provider="ollama", model="coder", allow_unverified=True
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "reviewer.md").write_text("---\nmodel: opus\n---\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-native-config"))
    monkeypatch.setattr(runtime, "probe_connection", Mock(return_value={"ok": True}))
    monkeypatch.setattr(runtime.signal, "signal", Mock())
    launched = []

    def launch(cli, env):
        launched.append((list(cli), dict(env)))
        return 0

    monkeypatch.setattr(runtime, "run_cli_redacted", launch)
    monkeypatch.setattr(
        sys, "argv", ["provider_config.py", json.dumps(selected.model_dump()), "claude", "-p"]
    )

    assert runtime.main() == 0
    [(cli, env)] = launched
    assert cli[:3] == ["claude", "--setting-sources", "project"]
    assert env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "coder"
