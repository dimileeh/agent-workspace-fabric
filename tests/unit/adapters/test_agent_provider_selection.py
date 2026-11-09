"""Provider selection must survive the registry's native defaults."""

from unittest.mock import Mock

import pytest

from awf.adapters import registry  # noqa: F401
from awf.adapters.base import AgentDefaults
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS
from awf.adapters.registry_api import get_adapter
from awf.common.agent_execution import resolve_execution
from awf.db.enums import AgentRuntime


@pytest.mark.parametrize("agent", [AgentRuntime.codex, AgentRuntime.claude_code])
def test_registry_cannot_reinstall_native_effort(agent):
    selection = resolve_execution(
        agent=agent.value, provider="ollama", model="qwen3-coder", allow_unverified=True
    )
    adapter = get_adapter(
        agent, runner=Mock(), defaults=DEFAULT_AGENT_DEFAULTS[agent], execution=selection
    )
    assert adapter.get_provider(None) == "ollama"
    assert adapter.default_model == "qwen3-coder"
    args = adapter._cli_args(model=None)
    assert "xhigh" not in str(args)
    assert "--effort" not in args
    assert adapter._default_effort is None
    native = get_adapter(agent, runner=Mock(), defaults=DEFAULT_AGENT_DEFAULTS[agent])
    assert "xhigh" in str(native._cli_args(model=None))
    inherited = get_adapter(
        agent,
        runner=Mock(),
        defaults=AgentDefaults(model="ignored", effort="xhigh", execution=selection),
    )
    assert inherited.get_provider(None) == "ollama"
    assert inherited._default_effort is None


async def test_runtime_contract_rejection_precedes_hosted_execution(tmp_path):
    from awf.adapters.base import AgentRunError
    from awf.common.commands import FakeCommandRunner

    selected = resolve_execution(
        agent="codex", provider="ollama", model="qwen3-coder", allow_unverified=True
    )
    executor = Mock()
    adapter = get_adapter(
        "codex", runner=FakeCommandRunner(), execution=selected, runtime_executor=executor
    )
    with pytest.raises((ValueError, AgentRunError), match="EXECUTION_RUNTIME_CONTRACT_UNSUPPORTED"):
        await adapter.run(
            compose_project="ws",
            compose_file=tmp_path / "compose.yml",
            prompt="fix",
            workspace_id="ws",
        )
    executor.execute.assert_not_called()


async def test_local_execution_uses_isolated_runtime_wrapper_and_no_native_credential_names(
    tmp_path,
):

    from awf.common.commands import FakeCommandRunner

    runner = FakeCommandRunner()
    selected = resolve_execution(
        agent="codex", provider="ollama", model="qwen3-coder", allow_unverified=True
    )
    adapter = get_adapter(
        "codex",
        runner=runner,
        defaults=DEFAULT_AGENT_DEFAULTS[AgentRuntime.codex],
        execution=selected,
    )
    await adapter.run(
        compose_project="ws", compose_file=tmp_path / "compose.yml", prompt="fix", workspace_id="ws"
    )
    command = next(call for call in runner.calls if "/opt/awf/provider_config.py" in str(call))
    assert "EXECUTION_RUNTIME_CONTRACT_UNSUPPORTED" in str(command)
    assert "OPENAI_API_KEY" not in str(command)
    assert "xhigh" not in str(command)
    assert selected.model in str(command)


async def test_capable_hosted_executor_gets_secret_free_full_snapshot(tmp_path, monkeypatch):
    from awf.adapters.runtime_executor import AgentRuntimeExecResult
    from awf.common.commands import FakeCommandRunner
    from awf.runtime.hosted_delegation_payloads import _agent_start_payload

    class Capable:
        execution_contract_version = 1
        request = None

        async def execute(self, request):
            self.request = request
            return AgentRuntimeExecResult(returncode=0, stdout="ok", stderr="")

    selected = resolve_execution(
        agent="claude_code", provider="openrouter", model="anthropic/test", allow_unverified=True
    )
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  agent:\n    image: agent\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-opaque-secret")
    executor = Capable()
    adapter = get_adapter(
        "claude_code",
        runner=FakeCommandRunner(),
        defaults=DEFAULT_AGENT_DEFAULTS[AgentRuntime.claude_code],
        execution=selected,
        runtime_executor=executor,
    )
    await adapter.run(compose_project="ws", compose_file=compose, prompt="fix", workspace_id="ws")
    assert executor.request.execution == selected
    assert executor.request.effort is None
    assert executor.request.env_passthrough_names == ("OPENROUTER_API_KEY",)
    payload = _agent_start_payload(executor.request)
    assert payload["execution"] == selected.model_dump()
    assert "router-opaque-secret" not in str(payload)
    assert "xhigh" not in str(payload)


async def test_old_local_runtime_has_structured_contract_rejection(tmp_path):
    from awf.adapters.base import AgentRunError
    from awf.common.commands import FakeCommandRunner

    runner = FakeCommandRunner()
    runner.queue_result(returncode=78, stderr="EXECUTION_RUNTIME_CONTRACT_UNSUPPORTED\n")
    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    adapter = get_adapter("codex", runner=runner, execution=selected)
    with pytest.raises(AgentRunError) as failure:
        await adapter.run(
            compose_project="ws",
            compose_file=tmp_path / "compose.yml",
            prompt="fix",
            workspace_id="ws",
        )
    assert failure.value.reason_code == "EXECUTION_RUNTIME_CONTRACT_UNSUPPORTED"
    assert failure.value.details["provider"] == "ollama"
    assert failure.value.details["execution"] == selected.model_dump()
