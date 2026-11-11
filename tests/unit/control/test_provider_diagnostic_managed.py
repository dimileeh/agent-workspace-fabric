"""Managed-key diagnostics share one key between probe and run and never park."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from awf.adapters import registry  # noqa: F401
from awf.adapters.managed_credentials import ProviderCredentialRelease
from awf.api.schemas_providers import ProviderConnectionTestRequest
from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.common.config import Settings
from awf.control.executor import WorkspaceExecutor
from awf.control.executor.config import ExecutorConfig
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkerHeartbeatRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.provider_diagnostics import create_connection_test
from awf.service.workspaces import WorkspaceService

_MINTED = "sk-or-v1-diagnostic-minted-sentinel"


class _Lease:
    def __init__(self) -> None:
        self.released: list[bool] = []

    @property
    def env(self):
        return {"OPENROUTER_API_KEY": _MINTED}

    @property
    def secrets(self):
        return (_MINTED,)

    async def heartbeat(self) -> None:
        return None

    async def release(self, *, failed: bool) -> ProviderCredentialRelease:
        self.released.append(failed)
        return ProviderCredentialRelease(exhausted=False, details={"budget_usd": "1.000000"})


class _Broker:
    heartbeat_interval_seconds = 3600.0

    def __init__(self) -> None:
        self.lease = _Lease()
        self.purposes: list[str] = []

    async def acquire(self, *, workspace_id, selection, purpose):
        assert selection.managed_key
        self.purposes.append(purpose)
        return self.lease


async def _prepared(engine, tmp_path, broker, agent_result: CommandResult):
    factory = make_session_factory(engine)
    async with factory() as session:
        now = datetime.now(UTC)
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="managed",
            node_id="local",
            started_at=now,
            last_heartbeat_at=now,
            poll_interval_seconds=5,
            execution_contract_version=3,
        )
        await session.commit()
    response = await create_connection_test(
        WorkspaceService(
            factory,
            settings=Settings(_env_file=None, openrouter_management_key="sk-or-mgmt-diag"),
        ),
        ProviderConnectionTestRequest(agent="codex", provider="openrouter", model="m/x"),
        idempotency_key="managed-diagnostic",
    )
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(response.workspace_id)
        assert ws.task_policy["provider_budget"]["limit_usd"] == 1.0
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="test")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="test")
        await session.commit()

    async def run(args, **kwargs):
        if "/opt/awf/provider_config.py" in args and "--probe-json" not in args:
            return agent_result
        return CommandResult(returncode=0, stdout="coding-cycle-v1", stderr="")

    runner = SimpleNamespace(run=create_autospec(AsyncioSubprocessRunner().run, side_effect=run))
    executor = WorkspaceExecutor(
        session_factory=factory,
        runner=runner,
        compose=SimpleNamespace(),
        validation=SimpleNamespace(),
        pr_creator=SimpleNamespace(),
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees", compose_projects_root=tmp_path / "compose"
        ),
        credential_broker=broker,
    )
    return factory, response, executor


async def test_one_key_serves_the_probe_and_the_run_and_is_released_once(
    engine, tmp_path, monkeypatch
):
    broker = _Broker()
    seen: list[dict | None] = []

    async def identity(executor, ws, compose_file, *, env=None):
        seen.append(env)
        return {"cli_version": "codex 0.162.0", "runtime_image": "sha256:actual"}

    monkeypatch.setattr("awf.control.executor.provider_diagnostics.runtime_identity", identity)
    factory, response, executor = await _prepared(
        engine, tmp_path, broker, CommandResult(returncode=0, stdout="no witness", stderr="")
    )

    await executor.execute(response.workspace_id)

    assert broker.purposes == ["provider_diagnostic"]
    assert seen == [{"OPENROUTER_API_KEY": _MINTED}]
    assert broker.lease.released == [False]
    agent_call = next(
        call
        for call in executor._runner.run.await_args_list
        if "/opt/awf/provider_config.py" in call.args[0] and "--probe-json" not in call.args[0]
    )
    assert "OPENROUTER_API_KEY" in agent_call.args[0]
    assert _MINTED not in " ".join(agent_call.args[0])
    assert agent_call.kwargs["env"]["OPENROUTER_API_KEY"] == _MINTED


@pytest.mark.parametrize("stderr", ["PROVIDER_HTTP_402", "402 Payment Required"])
async def test_exhaustion_is_a_terminal_diagnostic_outcome(engine, tmp_path, monkeypatch, stderr):
    broker = _Broker()

    async def identity(*_args, **_kwargs):
        return {"cli_version": "codex 0.162.0", "runtime_image": "sha256:actual"}

    monkeypatch.setattr("awf.control.executor.provider_diagnostics.runtime_identity", identity)
    factory, response, executor = await _prepared(
        engine, tmp_path, broker, CommandResult(returncode=1, stdout="", stderr=stderr)
    )

    await executor.execute(response.workspace_id)

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
    assert ws.status == "failed"
    result = ws.task_policy["provider_diagnostic"]["result"]
    assert result["reason_code"] == "PROVIDER_BUDGET_EXHAUSTED"
    assert result["execution_reason_code"] == "PROVIDER_BUDGET_EXHAUSTED"
    assert broker.lease.released == [True]
