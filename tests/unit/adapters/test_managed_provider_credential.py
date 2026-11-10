"""Managed OpenRouter keys reach the agent only through the exec environment."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from awf.adapters import registry  # noqa: F401
from awf.adapters.base import AgentRunError
from awf.adapters.managed_credentials import (
    ProviderCredentialError,
    ProviderCredentialRelease,
)
from awf.adapters.provider_failures import is_budget_exhausted_output
from awf.adapters.registry_api import get_adapter
from awf.common.agent_execution import resolve_execution
from awf.common.commands import CommandResult, FakeCommandRunner

_MINTED = "sk-or-v1-minted-adapter-sentinel-77"
_MANAGEMENT = "sk-or-mgmt-adapter-sentinel-88"
_BUDGET = {"budget_usd": "1.000000", "spend_usd": "1.000000", "remaining_usd": "0.000000"}


def _selection(agent: str = "codex", *, managed: bool = True):
    return resolve_execution(
        agent=agent,
        provider="openrouter",
        model="moonshotai/kimi-k2.7-code",
        allow_unverified=True,
        managed_openrouter=managed,
    )


class _Lease:
    def __init__(self, *, exhausted: bool = False, final_usage_read: bool | None = None) -> None:
        self.released: list[bool] = []
        self.heartbeats = 0
        self._exhausted = exhausted
        self._details = dict(_BUDGET)
        if final_usage_read is not None:
            self._details["final_usage_read"] = final_usage_read

    @property
    def env(self):
        return {"OPENROUTER_API_KEY": _MINTED}

    @property
    def secrets(self):
        return (_MINTED,)

    async def heartbeat(self) -> None:
        self.heartbeats += 1

    async def release(self, *, failed: bool) -> ProviderCredentialRelease:
        self.released.append(failed)
        return ProviderCredentialRelease(exhausted=self._exhausted, details=dict(self._details))


class _Broker:
    heartbeat_interval_seconds = 0.01

    def __init__(self, lease: _Lease | None = None, error: Exception | None = None) -> None:
        self.lease = lease or _Lease()
        self.error = error
        self.acquired: list[dict] = []

    async def acquire(self, *, workspace_id, selection, purpose):
        self.acquired.append({"workspace_id": workspace_id, "purpose": purpose})
        if self.error is not None:
            raise self.error
        return self.lease


def _adapter(runner, broker, *, agent: str = "codex", selection=None, **kwargs):
    return get_adapter(
        agent,
        runner=runner,
        execution=selection or _selection(agent),
        credential_broker=broker,
        **kwargs,
    )


async def _run(adapter, tmp_path, **kwargs):
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  agent:\n    image: agent\n")
    return await adapter.run(
        compose_project="awf_ws",
        compose_file=compose,
        prompt="fix",
        workspace_id="ws_managed",
        **kwargs,
    )


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
async def test_minted_key_is_passed_by_name_and_only_in_the_exec_environment(
    tmp_path, monkeypatch, agent
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "static-worker-key-should-not-win")
    runner = FakeCommandRunner()
    broker = _Broker()

    await _run(_adapter(runner, broker, agent=agent), tmp_path)

    (call,) = runner.calls
    assert call.args[call.args.index("-e") + 1] == "OPENROUTER_API_KEY"
    assert "OPENROUTER_API_KEY=" not in " ".join(call.args)
    assert _MINTED not in " ".join(call.args) and _MANAGEMENT not in " ".join(call.args)
    assert call.env is not None and call.env["OPENROUTER_API_KEY"] == _MINTED
    assert broker.acquired == [{"workspace_id": "ws_managed", "purpose": "agent"}]
    assert broker.lease.released == [False]


async def test_release_runs_when_the_run_fails_or_is_cancelled(tmp_path):
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="boom")
    broker = _Broker()
    with pytest.raises(AgentRunError) as raised:
        await _run(_adapter(runner, broker), tmp_path)
    assert raised.value.reason_code != "PROVIDER_BUDGET_EXHAUSTED"
    assert broker.lease.released == [True]

    hanging = FakeCommandRunner()
    hanging.queue_hang()
    broker = _Broker()
    task = asyncio.create_task(_run(_adapter(hanging, broker), tmp_path))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.lease.released == [True]
    assert broker.lease.heartbeats >= 1


async def test_a_failing_heartbeat_keeps_beating_until_release(tmp_path):
    class _FlakyLease(_Lease):
        async def heartbeat(self) -> None:
            self.heartbeats += 1
            if self.heartbeats == 1:
                # e.g. asyncpg's OSError while Postgres restarts (not a SQLAlchemy error).
                raise OSError("connection refused")

    hanging = FakeCommandRunner()
    hanging.queue_hang()
    broker = _Broker(_FlakyLease())
    task = asyncio.create_task(_run(_adapter(hanging, broker), tmp_path))
    for _ in range(200):
        if broker.lease.heartbeats >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert broker.lease.heartbeats >= 3
    assert broker.lease.released == [True]


@pytest.mark.parametrize(
    ("stderr", "exhausted"),
    [
        ("PROVIDER_HTTP_402", False),
        ("Error: 402 Payment Required", False),
        ("insufficient credits for this key", False),
        ("model refused the request", True),
    ],
)
async def test_budget_exhaustion_maps_to_a_non_retryable_park_code(tmp_path, stderr, exhausted):
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr=stderr)
    broker = _Broker(_Lease(exhausted=exhausted))

    with pytest.raises(AgentRunError) as raised:
        await _run(_adapter(runner, broker), tmp_path)

    error = raised.value
    assert error.reason_code == "PROVIDER_BUDGET_EXHAUSTED"
    assert error.details["retryable"] is False
    assert error.details["provider_budget"] == _BUDGET
    assert "provider_recovery" not in error.details
    assert _MINTED not in str(error)


@pytest.mark.parametrize(
    ("stdout", "stderr", "final_usage_read"),
    [
        # The authoritative key read says the key still has budget.
        ("", "HTTP 402 Payment Required", True),
        # Without a key read, the transcript (e.g. payment code under edit) is ignored.
        ("diff: raise PaymentRequired('402 payment required')", "tests failed", False),
        # Only the tail of stderr is trusted, not an early unrelated mention.
        ("", "fixture: 402 Payment Required\n" + "test output line\n" * 400, False),
    ],
)
async def test_payment_text_alone_never_relabels_an_unrelated_failure(
    tmp_path, stdout, stderr, final_usage_read
):
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stdout=stdout, stderr=stderr)
    broker = _Broker(_Lease(exhausted=False, final_usage_read=final_usage_read))

    with pytest.raises(AgentRunError) as raised:
        await _run(_adapter(runner, broker), tmp_path)

    assert raised.value.reason_code != "PROVIDER_BUDGET_EXHAUSTED"
    assert broker.lease.released == [True]


async def test_a_failed_key_read_falls_back_to_the_stderr_tail(tmp_path):
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=1, stderr="test output line\n" * 400 + "HTTP 402 Payment Required"
    )
    broker = _Broker(_Lease(exhausted=False, final_usage_read=False))

    with pytest.raises(AgentRunError) as raised:
        await _run(_adapter(runner, broker), tmp_path)

    assert raised.value.reason_code == "PROVIDER_BUDGET_EXHAUSTED"


async def test_timeouts_keep_their_watchdog_classification(tmp_path):
    runner = FakeCommandRunner()
    runner.queue_result(returncode=124, stderr="", reason_code="COMMAND_TIMEOUT")
    broker = _Broker(_Lease(exhausted=True))
    with pytest.raises(AgentRunError) as raised:
        await _run(_adapter(runner, broker), tmp_path)
    assert raised.value.reason_code == "AGENT_TIMEOUT"


@pytest.mark.parametrize(
    "reason",
    [
        "PROVIDER_BUDGET_EXHAUSTED",
        "PROVIDER_MANAGED_KEY_UNAVAILABLE",
        "PROVIDER_MANAGEMENT_KEY_MISSING",
    ],
)
async def test_acquire_failures_stop_before_any_command(tmp_path, reason):
    runner = FakeCommandRunner()
    broker = _Broker(error=ProviderCredentialError(reason, details={"provider_budget": _BUDGET}))

    with pytest.raises(AgentRunError) as raised:
        await _run(_adapter(runner, broker), tmp_path)

    assert raised.value.reason_code == reason
    assert raised.value.details["retryable"] is False
    assert raised.value.details["provider_budget"] == _BUDGET
    assert runner.calls == []


async def test_managed_selection_without_a_broker_fails_closed(tmp_path):
    runner = FakeCommandRunner()
    with pytest.raises(AgentRunError) as raised:
        await _run(_adapter(runner, None), tmp_path)
    assert raised.value.reason_code == "PROVIDER_MANAGEMENT_KEY_MISSING"
    assert runner.calls == []


async def test_hosted_managed_runs_are_rejected_before_minting(tmp_path):
    executor = Mock()
    executor.execution_contract_version = 1
    broker = _Broker()
    with pytest.raises(AgentRunError) as raised:
        await _run(_adapter(FakeCommandRunner(), broker, runtime_executor=executor), tmp_path)
    assert raised.value.reason_code == "PROVIDER_MANAGED_KEY_HOSTED_UNSUPPORTED"
    assert broker.acquired == []
    executor.execute.assert_not_called()


async def test_a_caller_owned_lease_is_used_without_acquiring_or_releasing(tmp_path):
    runner = FakeCommandRunner()
    broker = _Broker()
    lease = _Lease()

    await _run(_adapter(runner, broker), tmp_path, credential_lease=lease)

    assert broker.acquired == [] and lease.released == []
    assert runner.calls[0].env["OPENROUTER_API_KEY"] == _MINTED


@pytest.mark.parametrize(
    "selection",
    [
        _selection(managed=False),
        resolve_execution(
            agent="codex", provider="ollama", model="qwen3-coder", allow_unverified=True
        ),
        resolve_execution(agent="codex"),
    ],
)
async def test_static_ollama_and_native_selections_never_use_the_broker(
    tmp_path, monkeypatch, selection
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "static-worker-key")
    runner = FakeCommandRunner()
    broker = _Broker()

    await _run(_adapter(runner, broker, selection=selection), tmp_path)

    assert broker.acquired == []
    assert runner.calls[0].env is None
    passthrough = runner.calls[0].args
    if selection.provider == "openrouter":
        assert "OPENROUTER_API_KEY" in passthrough
    elif selection.third_party:
        assert "OPENROUTER_API_KEY" not in passthrough


async def test_runtime_probe_receives_the_same_exec_environment(tmp_path):
    from awf.service.provider_compatibility import guard_runtime_compatibility

    runner = FakeCommandRunner()
    runner.respond_when(lambda args: "--capability" in args, stdout="coding-cycle-v1\n")
    runner.respond_when(lambda args: "--version" in args, stdout="codex-cli 0.162.0\n")
    runner.respond_when(lambda args: args[-2:] == ["-q", "agent"], stdout="container\n")
    runner.respond_when(lambda args: args[:2] == ["docker", "inspect"], stdout="sha256:abc\n")
    runner.respond_when(
        lambda args: "--probe-json" in args, stdout='{"ok": true, "model_version": null}'
    )
    adapter = _adapter(runner, _Broker())
    adapter.require_compatibility_evidence = True

    with pytest.raises(ValueError, match="AGENT_PROVIDER_MODEL_UNVERIFIED"):
        await guard_runtime_compatibility(
            adapter,
            compose_project="awf_ws",
            compose_file=tmp_path / "compose.yml",
            workspace_id="ws_managed",
            env={"OPENROUTER_API_KEY": _MINTED},
        )

    probe = next(call for call in runner.calls if "--probe-json" in call.args)
    assert probe.args[probe.args.index("-e") + 1] == "OPENROUTER_API_KEY"
    assert _MINTED not in " ".join(probe.args)
    assert probe.env["OPENROUTER_API_KEY"] == _MINTED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("PROVIDER_HTTP_402", True),
        ("HTTP 402 Payment Required", True),
        ("status: 402", True),
        ("insufficient credits", True),
        ("Key limit exceeded", True),
        ("processed 402 files", False),
        ("rate limited (429)", False),
        ("", False),
    ],
)
def test_budget_exhaustion_output_markers(text, expected):
    assert is_budget_exhausted_output(text) is expected


def test_exhaustion_error_result_redacts_the_lease_secret():
    from awf.adapters.managed_credentials import budget_exhausted_error

    original = AgentRunError(
        agent="codex",
        result=CommandResult(returncode=1, stdout=f"echo {_MINTED}", stderr="402 Payment Required"),
        reason_code="AGENT_CLI_FAILED",
    )
    adapter = _adapter(FakeCommandRunner(), _Broker())
    mapped = budget_exhausted_error(
        adapter, original, ProviderCredentialRelease(exhausted=True), secrets=(_MINTED,)
    )
    assert _MINTED not in mapped.result.stdout and _MINTED not in str(mapped)
    assert mapped.details["source_reason_code"] == "AGENT_CLI_FAILED"


class _BlockingLease(_Lease):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.finished = False

    async def release(self, *, failed: bool) -> ProviderCredentialRelease:
        self.released.append(failed)
        await self.gate.wait()
        self.finished = True
        return ProviderCredentialRelease(exhausted=False)


@pytest.mark.parametrize(
    ("result", "masked"),
    [
        ({"returncode": 124, "reason_code": "COMMAND_TIMEOUT"}, "AGENT_TIMEOUT"),
        ({"returncode": 1, "stderr": "boom"}, None),
    ],
)
async def test_cancelling_during_release_keeps_the_key_release_and_timeout_tag(
    tmp_path, result, masked
):
    runner = FakeCommandRunner()
    runner.queue_result(**result)
    lease = _BlockingLease()
    task = asyncio.create_task(_run(_adapter(runner, _Broker(lease)), tmp_path))
    while not lease.released:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert getattr(raised.value, "agent_reason_code", None) == masked
    lease.gate.set()
    await asyncio.sleep(0.01)
    assert lease.finished is True
