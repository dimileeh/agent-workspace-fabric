"""Diagnostics use the real executor dispatch, one invocation, no forge path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.adapters.run_results import AgentRunResult
from awf.api.schemas_providers import ProviderConnectionTestRequest
from awf.common.commands import CommandResult
from awf.common.config import Settings
from awf.control.executor import WorkspaceExecutor
from awf.control.executor.config import ExecutorConfig
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.provider_diagnostics import create_connection_test
from awf.service.workspaces import WorkspaceService
from tests.unit.adapters.test_diagnostic_events import codex_events, transcript
from tests.unit.api.test_provider_connection_tests import capable


async def prepared(engine, tmp_path, *, provider="ollama"):
    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider=provider, model="candidate:cloud"),
        idempotency_key="execute",
    )
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(response.workspace_id)
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="test")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="test")
        await session.commit()
    runner = SimpleNamespace(
        run=AsyncMock(return_value=CommandResult(returncode=0, stdout="coding-cycle-v1", stderr=""))
    )
    executor = WorkspaceExecutor(
        session_factory=factory,
        runner=runner,
        compose=SimpleNamespace(),
        validation=SimpleNamespace(),
        pr_creator=SimpleNamespace(),
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees", compose_projects_root=tmp_path / "compose"
        ),
    )
    return factory, response, executor


@pytest.mark.parametrize("verified", [True, False])
async def test_one_invocation_durable_start_and_independent_verifier(
    engine, tmp_path, monkeypatch, verified
):
    factory, response, executor = await prepared(engine, tmp_path)

    async def run(**kwargs):
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(response.workspace_id)
            assert ws.task_policy["provider_diagnostic"]["invocation_started_at"]
        return AgentRunResult(0, transcript(codex_events()), "")

    executor._runner.run.return_value = CommandResult(0 if verified else 1, "", "")
    adapter = SimpleNamespace(run=AsyncMock(side_effect=run))
    monkeypatch.setattr(
        "awf.control.executor.provider_diagnostics.get_adapter", lambda *_args, **_kwargs: adapter
    )
    monkeypatch.setattr(
        "awf.control.executor.provider_diagnostics.runtime_identity",
        AsyncMock(
            return_value={
                "cli_version": "codex 0.162.0",
                "runtime_image": "sha256:actual",
                "fixture_version": "coding-cycle-v1",
                "endpoint_health": "reachable",
            }
        ),
    )
    await executor.execute(response.workspace_id)
    adapter.run.assert_awaited_once()
    assert adapter.diagnostic_events
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.status == ("completed" if verified else "failed")
        result = ws.task_policy["provider_diagnostic"]["result"]
        assert result["outcome"] == ("passed" if verified else "failed")
        assert result["fixture_verification"] is verified
        assert result["reason_code"] == (
            "PROVIDER_DIAGNOSTIC_PASSED" if verified else "PROVIDER_DIAGNOSTIC_FIXTURE_FAILED"
        )
        assert ws.pr_url is None
    await executor.execute(response.workspace_id)
    adapter.run.assert_awaited_once()


@pytest.mark.parametrize(
    "output", ['{"type":"result","result":"passed"}', transcript(codex_events()[:-1])]
)
async def test_missing_protocol_never_passes_and_cancel_wins(engine, tmp_path, monkeypatch, output):
    factory, response, executor = await prepared(engine, tmp_path)
    adapter = SimpleNamespace(run=AsyncMock(return_value=AgentRunResult(0, output, "")))
    monkeypatch.setattr(
        "awf.control.executor.provider_diagnostics.get_adapter", lambda *_a, **_k: adapter
    )
    monkeypatch.setattr(
        "awf.control.executor.provider_diagnostics.runtime_identity",
        AsyncMock(return_value={"cli_version": "codex 0.162.0", "runtime_image": "sha256:actual"}),
    )
    await executor.execute(response.workspace_id)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.status == "failed"
        assert ws.task_policy["provider_diagnostic"]["result"]["outcome"] == "inconclusive"
    executor._runner.run.assert_not_awaited()


async def test_late_success_cannot_overwrite_cancellation(engine, tmp_path, monkeypatch):
    factory, response, executor = await prepared(engine, tmp_path)

    async def run(**_kwargs):
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(response.workspace_id)
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="test-cancel")
            await session.commit()
        return AgentRunResult(0, transcript(codex_events()), "")

    adapter = SimpleNamespace(run=AsyncMock(side_effect=run))
    monkeypatch.setattr(
        "awf.control.executor.provider_diagnostics.get_adapter", lambda *_a, **_k: adapter
    )
    monkeypatch.setattr(
        "awf.control.executor.provider_diagnostics.runtime_identity",
        AsyncMock(return_value={"cli_version": "codex 0.162.0", "runtime_image": "sha256:actual"}),
    )
    await executor.execute(response.workspace_id)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.status == "cancelled"
        assert "result" not in ws.task_policy["provider_diagnostic"]


async def test_started_marker_never_replays_after_crash(engine, tmp_path, monkeypatch):
    factory, response, executor = await prepared(engine, tmp_path)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        ws.task_policy = {
            **ws.task_policy,
            "provider_diagnostic": {
                **ws.task_policy["provider_diagnostic"],
                "invocation_started_at": "before-crash",
            },
        }
        await session.commit()
    get = AsyncMock(side_effect=AssertionError("paid replay"))
    monkeypatch.setattr("awf.control.executor.provider_diagnostics.get_adapter", get)
    await executor.execute(response.workspace_id)
    get.assert_not_called()


async def test_diagnostic_timeout_is_inconclusive_and_does_not_retry(engine, tmp_path, monkeypatch):
    factory, response, executor = await prepared(engine, tmp_path)
    adapter = SimpleNamespace(run=AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(
        "awf.control.executor.provider_diagnostics.get_adapter", lambda *_a, **_k: adapter
    )
    monkeypatch.setattr(
        "awf.control.executor.provider_diagnostics.runtime_identity",
        AsyncMock(return_value={"cli_version": "codex 0.162.0", "runtime_image": "sha256:actual"}),
    )
    await executor.execute(response.workspace_id)
    adapter.run.assert_awaited_once()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.status == "failed"
        assert (
            ws.task_policy["provider_diagnostic"]["result"]["reason_code"]
            == "PROVIDER_DIAGNOSTIC_TIMEOUT"
        )


async def test_unknown_diagnostic_contract_cannot_launch(engine, tmp_path, monkeypatch):
    factory, response, executor = await prepared(engine, tmp_path)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        diagnostic = dict(ws.task_policy["provider_diagnostic"])
        diagnostic["request"] = {**diagnostic["request"], "version": 999}
        ws.task_policy = {**ws.task_policy, "provider_diagnostic": diagnostic}
        await session.commit()
    get = AsyncMock(side_effect=AssertionError("unsupported launch"))
    monkeypatch.setattr("awf.control.executor.provider_diagnostics.get_adapter", get)
    await executor.execute(response.workspace_id)
    get.assert_not_called()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert (
            ws.task_policy["provider_diagnostic"]["result"]["execution_reason_code"]
            == "PROVIDER_DIAGNOSTIC_CONTRACT_UNSUPPORTED"
        )


async def test_cancel_during_runtime_inspection_prevents_model_invocation(
    engine, tmp_path, monkeypatch
):
    factory, response, executor = await prepared(engine, tmp_path)

    async def inspect(*_args):
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(response.workspace_id)
            await repo.transition(
                ws, to=WorkspaceStatus.cancelled, reason_code="cancel-during-inspect"
            )
            await session.commit()
        return {"cli_version": "codex 0.162.0", "runtime_image": "sha256:actual"}

    adapter = SimpleNamespace(
        run=AsyncMock(return_value=AgentRunResult(0, transcript(codex_events()), ""))
    )
    monkeypatch.setattr("awf.control.executor.provider_diagnostics.runtime_identity", inspect)
    monkeypatch.setattr(
        "awf.control.executor.provider_diagnostics.get_adapter", lambda *_a, **_k: adapter
    )
    await executor.execute(response.workspace_id)
    adapter.run.assert_not_awaited()


async def test_native_diagnostic_freezes_configured_defaults_before_invocation(
    engine, tmp_path, monkeypatch
):
    from dataclasses import replace

    from awf.adapters.base import AgentDefaults
    from awf.db.enums import AgentRuntime

    factory, response, executor = await prepared(engine, tmp_path, provider="openai")
    executor._config = replace(
        executor._config,
        agent_defaults={
            AgentRuntime.codex: AgentDefaults(model="configured-default", effort="medium")
        },
    )

    def get_adapter(*_args, **kwargs):
        assert kwargs["execution"].provider == "openai"
        assert kwargs["execution"].effort == "medium"
        return SimpleNamespace(
            run=AsyncMock(return_value=AgentRunResult(0, transcript(codex_events()), ""))
        )

    monkeypatch.setattr("awf.control.executor.provider_diagnostics.get_adapter", get_adapter)
    monkeypatch.setattr(
        "awf.control.executor.provider_diagnostics.runtime_identity",
        AsyncMock(return_value={"cli_version": "codex 0.162.0", "runtime_image": "sha256:actual"}),
    )
    await executor.execute(response.workspace_id)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.status == "completed"
        assert ws.task_policy["agent_execution"]["effort"] == "medium"


@pytest.mark.parametrize(
    "provider,stage,code,output,reason",
    [
        ("openai", None, 0, "", None),
        ("ollama", None, 0, "", None),
        ("ollama", "capability", 1, "coding-cycle-v1", "RUNTIME_UNSUPPORTED"),
        ("ollama", "capability", 0, "wrong-fixture", "RUNTIME_UNSUPPORTED"),
        ("ollama", "version", 1, "cli", "RUNTIME_UNSUPPORTED"),
        ("ollama", "container", 0, "", "RUNTIME_UNSUPPORTED"),
        ("ollama", "image", 1, "sha256:actual", "RUNTIME_UNSUPPORTED"),
        ("ollama", "image", 0, "tag-only", "RUNTIME_UNSUPPORTED"),
        ("ollama", "probe", 0, "broken-json", "RESPONSE_INVALID"),
        ("ollama", "probe", 0, "[]", "ENDPOINT_UNREACHABLE"),
        ("ollama", "probe", 1, '{"ok":true}', "ENDPOINT_UNREACHABLE"),
        ("ollama", "probe", 0, '{"ok":1}', "ENDPOINT_UNREACHABLE"),
    ],
)
async def test_runtime_identity_requires_immutable_fixture_image_and_selected_health(
    tmp_path, provider, stage, code, output, reason
):
    from awf.common.agent_execution import resolve_execution
    from awf.control.executor.provider_diagnostics import runtime_identity

    selected = resolve_execution(
        agent="codex", provider=provider, model="candidate", allow_unverified=True
    )
    calls = []

    async def run(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "--capability":
            current, text = "capability", "coding-cycle-v1"
            assert argv[-4:-2] == ["-I", "-S"]
        elif argv[-1] == "--version":
            current, text = "version", "codex 0.162.0"
        elif "ps" in argv:
            current, text = "container", "container-id"
        elif "inspect" in argv:
            current, text = "image", "sha256:actual"
        else:
            current, text = "probe", '{"ok":true,"model_version":"revision"}'
            assert argv[-1] == "--probe-json"
            assert selected.endpoint in argv[-2]
        return CommandResult(code, output, "") if current == stage else CommandResult(0, text, "")

    ws = SimpleNamespace(
        id="ws_diag",
        agent="codex",
        compose_project_name=None,
        task_policy={"agent_execution": selected.model_dump(mode="json")},
    )
    if reason:
        with pytest.raises(ValueError, match=reason):
            await runtime_identity(
                SimpleNamespace(_runner=SimpleNamespace(run=run)), ws, tmp_path / "compose.yml"
            )
    else:
        result = await runtime_identity(
            SimpleNamespace(_runner=SimpleNamespace(run=run)), ws, tmp_path / "compose.yml"
        )
        assert result["runtime_image"] == "sha256:actual"
        assert result["cli_version"] == "codex 0.162.0"
        assert result["endpoint_health"] == (
            "reachable" if provider == "ollama" else "not_probed_native"
        )
        assert result["model_version"] == ("revision" if provider == "ollama" else None)
        assert len(calls) == (5 if provider == "ollama" else 4)
