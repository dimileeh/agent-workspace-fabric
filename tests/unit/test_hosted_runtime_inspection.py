"""Inspection uses execution authority, never the inherited control daemon."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.control.executor.monitor_handoff import _inspect_compose_runtime
from awf.control.executor.monitor_resume_runtime import inspect_resume_runtime
from awf.control.executor.runtime_setup_replay import capture_agent_container_identity
from awf.runtime.inspection import RuntimeInspector
from awf.runtime.pr_monitor_runner import agent_service_recovery
from awf.service import worker
from tests.unit.service.test_worker import _settings
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_authority import bound_workspace as bound_workspace
from tests.unit.test_hosted_runtime_consumers import consumers


@pytest.fixture
def docker_probe(tmp_path, monkeypatch):
    binary = tmp_path / "docker"
    log = tmp_path / "calls.jsonl"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as output:\n"
        " output.write(json.dumps({'args': sys.argv[1:], 'host': os.getenv('DOCKER_HOST'), "
        "'secret': os.getenv('HOST_ONLY_SECRET')}) + '\\n')\n"
        "if sys.argv[1] == 'ps':\n"
        " print(json.dumps({'ID': 'execution-agent', 'State': 'running'}))\n"
        "else:\n"
        " print(json.dumps([{'State': {'Status': 'running', 'StartedAt': 'now'}, "
        "'Config': {'Labels': {'com.docker.compose.service': 'agent'}}}]))\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.defpath)
    monkeypatch.setenv("DOCKER_HOST", "unix:///synthetic-control.sock")
    monkeypatch.setenv("HOST_ONLY_SECRET", "synthetic-must-not-inherit")
    return log


def _calls(log: Path):
    return [json.loads(line) for line in log.read_text().splitlines()]


async def test_explicit_inspector_uses_same_environment_for_ps_and_inspect(docker_probe):
    environment = {"PATH": os.environ["PATH"], "DOCKER_HOST": "unix:///synthetic-execution.sock"}
    before = dict(os.environ)
    result = await RuntimeInspector(environment=environment).inspect("awf_ws_exact")
    assert result.stack_state == "running"
    assert [call["args"][0] for call in _calls(docker_probe)] == ["ps", "inspect"]
    assert all(call["host"] == environment["DOCKER_HOST"] for call in _calls(docker_probe))
    assert all(call["secret"] is None for call in _calls(docker_probe))
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    "probe", [_inspect_compose_runtime, inspect_resume_runtime, capture_agent_container_identity]
)
async def test_nested_vm_recovery_inspection_uses_consumer_endpoint(
    tmp_path, monkeypatch, docker_probe, probe
):
    scope, _ = consumers(tmp_path, monkeypatch)
    scope.base_environment["DOCKER_HOST"] = "unix:///synthetic-execution.sock"
    before = dict(os.environ)
    async with scope.scope("ws_exact"):
        await probe("awf_ws_exact")
    assert [call["args"][0] for call in _calls(docker_probe)] == ["ps", "inspect"]
    assert all(call["host"] == "unix:///synthetic-execution.sock" for call in _calls(docker_probe))
    assert all(call["secret"] is None for call in _calls(docker_probe))
    assert dict(os.environ) == before


async def test_default_inspector_keeps_local_inherited_endpoint(docker_probe):
    await RuntimeInspector().inspect("awf_ws_local")
    assert all(call["host"] == "unix:///synthetic-control.sock" for call in _calls(docker_probe))


async def test_concurrent_scopes_and_child_probes_do_not_leak_endpoint(
    tmp_path, monkeypatch, docker_probe
):
    first, _ = consumers(tmp_path, monkeypatch)
    second, _ = consumers(tmp_path, monkeypatch)
    first.base_environment["DOCKER_HOST"] = "unix:///one.sock"
    second.base_environment["DOCKER_HOST"] = "unix:///two.sock"
    gate = asyncio.Event()

    async def probe(scope, workspace):
        async with scope.scope(workspace):
            await gate.wait()
            await asyncio.create_task(RuntimeInspector().inspect("awf_" + workspace))

    tasks = [
        asyncio.create_task(probe(first, "ws_one")),
        asyncio.create_task(probe(second, "ws_two")),
    ]
    gate.set()
    await asyncio.gather(*tasks)
    assert (
        sorted(call["host"] for call in _calls(docker_probe))
        == ["unix:///one.sock"] * 2 + ["unix:///two.sock"] * 2
    )
    await RuntimeInspector().inspect("awf_ws_local")
    assert all(
        call["host"] == "unix:///synthetic-control.sock" for call in _calls(docker_probe)[-2:]
    )


@pytest.mark.parametrize("cleanup", [False, True])
async def test_vm_compose_monitor_timeout_and_cleanup_probes_use_execution_endpoint(
    tmp_path, monkeypatch, docker_probe, cleanup
):
    scope, _ = consumers(tmp_path, monkeypatch)
    scope.base_environment["DOCKER_HOST"] = "unix:///synthetic-execution.sock"
    monitor = SimpleNamespace(_deps=SimpleNamespace(adapter=SimpleNamespace(is_hosted=False)))
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    error = SimpleNamespace(
        reason_code="AGENT_IDLE_TIMEOUT",
        details={},
        result=SimpleNamespace(stdout="", stderr="timeout"),
    )
    kwargs = {
        "workspace_id": "ws_exact",
        "compose_project": "awf_ws_exact",
        "compose_file": compose,
        "exc": error,
        "restart_attempts": 0,
        "command_evidence": [],
    }
    async with scope.scope("ws_exact"):
        if cleanup:
            await agent_service_recovery._recover_monitor_agent_service_after_cleanup_error(
                monitor, **kwargs, operation_start_head=None
            )
        else:
            await agent_service_recovery._recover_monitor_agent_service_after_error(
                monitor, **kwargs
            )
    assert [call["args"][0] for call in _calls(docker_probe)] == ["ps", "inspect"]
    assert all(call["host"] == "unix:///synthetic-execution.sock" for call in _calls(docker_probe))


async def test_real_worker_and_driver_inspectors_share_configured_environment(
    tmp_path, monkeypatch, docker_probe
):
    from awf.hosted_node.runtime_consumers import WorkspaceConsumers
    from awf.hosted_node.schemas import Identity

    authority = AsyncMock()
    authority.receiver.config.host = None
    authority.receiver.identity = Identity(
        owner="owner/node", incarnation="123", recovery_generation=1, boot_epoch=2
    )
    scope = WorkspaceConsumers(authority, tmp_path)
    scope.base_environment["DOCKER_HOST"] = "unix:///stale-inherited.sock"
    runtime = worker.build_worker_runtime(
        replace(
            _settings(tmp_path), github_token=None, docker_host="unix:///synthetic-execution.sock"
        ),
        hosted=scope,
    )
    try:
        await runtime.worker._runtime_inspector.inspect("awf_ws_exact")
        await runtime.runtime_driver.runtime_inspector.inspect("awf_ws_exact")
        assert [call["args"][0] for call in _calls(docker_probe)] == ["ps", "inspect"] * 2
        assert all(
            call["host"] == "unix:///synthetic-execution.sock" for call in _calls(docker_probe)
        )
    finally:
        await runtime.engine.dispose()


async def test_hosted_api_lifespan_routes_runtime_read_to_execution_endpoint(
    bound_workspace, tmp_path, monkeypatch, docker_probe
):
    from httpx import ASGITransport, AsyncClient

    from awf.api import app as app_module
    from awf.common.config import Settings, get_settings
    from awf.db.models import Workspace
    from awf.hosted_node import runtime_worker
    from awf.hosted_node.runtime_consumers import WorkspaceConsumers
    from awf.service import config

    authority, _, factory = bound_workspace
    engine = factory.kw["bind"]
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.compose_project_name = "awf_ws_cloud_stable"
    scope = WorkspaceConsumers(authority, tmp_path)
    scope.base_environment["DOCKER_HOST"] = "unix:///synthetic-execution.sock"
    settings = Settings(api_token="synthetic-operator", hosted_node=authority.receiver.config)

    @asynccontextmanager
    async def host(*args):
        yield authority.receiver

    @asynccontextmanager
    async def runtime(*args):
        yield SimpleNamespace(consumers=scope)

    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(app_module, "make_engine", lambda _: engine)
    monkeypatch.setattr(app_module, "make_session_factory", lambda _: factory)
    monkeypatch.setattr(app_module, "start_hosted", host)
    monkeypatch.setattr(runtime_worker, "start_runtime", runtime)
    monkeypatch.setattr(config, "resolve_service_settings", lambda _: None)
    app = app_module.create_app(use_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    async with app_module._lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/v1/workspaces/ws_cloud_stable/runtime",
                headers={
                    "Authorization": "Bearer synthetic-operator",
                    **authority.receiver.identity.headers(),
                },
            )
        assert response.status_code == 200, response.text
    assert [call["args"][0] for call in _calls(docker_probe)] == ["ps", "inspect"]
    assert all(call["host"] == "unix:///synthetic-execution.sock" for call in _calls(docker_probe))
