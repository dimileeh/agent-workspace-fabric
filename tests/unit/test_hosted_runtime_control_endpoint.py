"""Public controls must clean the execution daemon, never an empty control daemon."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from awf.api import app as app_module
from awf.common.config import Settings, get_settings
from awf.db.models import Operation, Workspace, WorkspaceEvent
from awf.service import config, controls_helpers
from awf.service.workspaces import WorkspaceService
from tests.unit.mcp.test_mcp_control_contracts import _call
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_authority import bound_workspace as bound_workspace
from tests.unit.test_hosted_runtime_retained_view_lifecycle import runtime_settings
from tests.unit.test_hosted_runtime_warm_views import owned_workspace as owned_workspace

EXECUTION = "unix:///synthetic-execution.sock"
CONTROL = "unix:///synthetic-control.sock"


@pytest.fixture
def docker_control_probe(tmp_path, monkeypatch):
    log = tmp_path / "docker-calls.jsonl"
    mode = tmp_path / "docker-mode"
    mode.write_text("empty")
    binary = tmp_path / "docker"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"with open({str(log)!r}, 'a') as output:\n"
        " output.write(json.dumps({'args': sys.argv[1:], 'host': os.getenv('DOCKER_HOST'), "
        "'context': os.getenv('DOCKER_CONTEXT'), 'inherited_secret': 'HOST_ONLY_SECRET' in os.environ}) + '\\n')\n"
        f"mode = pathlib.Path({str(mode)!r}).read_text()\n"
        f"if os.getenv('DOCKER_HOST') == {EXECUTION!r}:\n"
        " if mode == 'failure':\n"
        "  print('synthetic execution daemon unavailable', file=sys.stderr)\n"
        "  sys.exit(1)\n"
        " if mode == 'remove-failure':\n"
        "  if sys.argv[1] in ('compose', 'rm'): sys.exit(1)\n"
        "  if sys.argv[1] == 'ps': print('execution-container')\n"
        " if mode == 'present' and sys.argv[1] == 'ps': print('execution-container')\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.defpath)
    monkeypatch.setenv("DOCKER_HOST", CONTROL)
    monkeypatch.setenv("DOCKER_CONTEXT", "synthetic-control-context")
    monkeypatch.setenv("HOST_ONLY_SECRET", "synthetic-not-a-credential")
    return log, mode


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@asynccontextmanager
async def running_app(owned_workspace, tmp_path, monkeypatch):
    authority, _, factory = owned_workspace
    service_settings = replace(
        await runtime_settings(owned_workspace, tmp_path, monkeypatch), docker_host=EXECUTION
    )
    settings = Settings(
        api_token="synthetic-operator",
        hosted_node=authority.receiver.config,
        work_dir=service_settings.work_dir,
        docker_host=EXECUTION,
    )
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.compose_project_name = "awf_ws_cloud_stable"
        row.compose_file_path = str(compose)
    engine = factory.kw["bind"]

    @asynccontextmanager
    async def host(*args):
        yield authority.receiver

    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(app_module, "make_engine", lambda _: engine)
    monkeypatch.setattr(app_module, "make_session_factory", lambda _: factory)
    monkeypatch.setattr(app_module, "start_hosted", host)
    monkeypatch.setattr(config, "resolve_service_settings", lambda _: service_settings)
    # Poison global settings as well: request/service settings are authoritative.
    monkeypatch.setattr(controls_helpers, "get_settings", lambda: Settings(work_dir=str(tmp_path)))
    app = app_module.create_app(use_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    async with (
        app_module._lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield (
            client,
            settings,
            factory,
            {
                "Authorization": "Bearer synthetic-operator",
                "Idempotency-Key": "synthetic-control-key",
                **authority.receiver.identity.headers(),
            },
        )


async def request_control(client, action, headers):
    path = "/v1/workspaces/ws_cloud_stable"
    if action == "destroy":
        return await client.delete(
            path, params={"force": True, "remove_worktree": False}, headers=headers
        )
    return await client.post(path + "/" + action, json={"stop_stack": True}, headers=headers)


@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
@pytest.mark.parametrize("mode", ["empty", "failure", "remove-failure"])
@pytest.mark.parametrize("surface", ["rest", "service", "mcp"])
@pytest.mark.parametrize("compose_present", [True, False], ids=["down", "labels"])
async def test_public_controls_use_execution_endpoint_without_false_release(
    owned_workspace,
    tmp_path,
    monkeypatch,
    docker_control_probe,
    action,
    mode,
    surface,
    compose_present,
):
    log, mode_file = docker_control_probe
    mode_file.write_text(mode)
    before = dict(os.environ)
    async with running_app(owned_workspace, tmp_path, monkeypatch) as (
        client,
        settings,
        factory,
        headers,
    ):
        if not compose_present:
            (tmp_path / "compose.yml").unlink()
        if surface == "rest":
            response = await request_control(client, action, headers)
            assert response.status_code == 200, response.text
            body = response.json()
        else:
            service = WorkspaceService(factory, settings=settings)
            kwargs = {"workspace_id": "ws_cloud_stable", "idempotency_key": "control-key"}
            if action == "destroy":
                kwargs.update(force=True, remove_worktree=False)
            if action == "cancel":
                kwargs["stop_stack"] = True
            if surface == "service":
                body = (await getattr(service, action + "_workspace")(**kwargs)).model_dump()
            else:
                from awf.mcp.server import build_mcp_server

                body = await _call(
                    build_mcp_server(service=service), f"awf_{action}_workspace", kwargs
                )
        assert body["operation_status"] == ("succeeded" if mode == "empty" else "failed")
        async with factory() as session:
            operation = await session.get(Operation, body["operation_id"])
            assert operation.status == body["operation_status"]
            released = list(
                await session.scalars(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == "ws_cloud_stable",
                        WorkspaceEvent.event_type == "workspace.terminal_runtime_released",
                    )
                )
            )
            if mode != "empty":
                assert not released
            else:
                assert released
    observed = calls(log)
    assert observed
    assert {call["host"] for call in observed} == {EXECUTION}
    assert all(call["context"] is None and not call["inherited_secret"] for call in observed)
    assert dict(os.environ) == before


async def test_direct_project_stop_uses_task_endpoint(docker_control_probe):
    from awf.runtime.inspection import inspection_environment

    log, mode = docker_control_probe
    mode.write_text("present")
    environment = {"PATH": os.environ["PATH"], "DOCKER_HOST": EXECUTION}
    with inspection_environment(environment):
        await controls_helpers.stop_project_containers("awf_ws_exact")
    assert [call["args"][0] for call in calls(log)] == ["ps", "stop"]
    assert {call["host"] for call in calls(log)} == {EXECUTION}


@pytest.mark.parametrize("caller", ["recovery", "post-merge"])
async def test_monitor_created_compose_managers_use_task_endpoint(
    tmp_path, monkeypatch, docker_control_probe, caller
):
    from awf.runtime.inspection import inspection_environment
    from awf.runtime.pr_monitor_runner import agent_service_recovery, lifecycle

    log, _ = docker_control_probe
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    monitor = SimpleNamespace(_work_dir=tmp_path)
    environment = {"PATH": os.environ["PATH"], "DOCKER_HOST": EXECUTION}
    with inspection_environment(environment):
        if caller == "post-merge":
            callback = lifecycle._completed_workspace_compose_teardown(
                monitor, compose_project="awf_ws_exact", compose_file=compose
            )
            result = await callback(
                SimpleNamespace(
                    workspace_id="ws_exact",
                    compose_project_name="awf_ws_exact",
                    compose_file_path=str(compose),
                )
            )
            assert result.status == "succeeded"
        else:
            monkeypatch.setattr(
                agent_service_recovery,
                "_raise_if_monitor_agent_service_recovery_was_superseded",
                AsyncMock(),
            )
            monkeypatch.setattr(
                agent_service_recovery,
                "_monitor_agent_service_restart_timeout_seconds",
                AsyncMock(return_value=1),
            )
            monkeypatch.setattr(
                agent_service_recovery,
                "replay_setup_after_monitor_agent_service_restart",
                AsyncMock(),
            )
            monkeypatch.setattr(
                agent_service_recovery._runtime_setup_replay,
                "capture_agent_container_identity",
                AsyncMock(return_value=None),
            )
            assert (
                await agent_service_recovery._restart_monitor_agent_service_or_fail(
                    monitor,
                    workspace_id="ws_exact",
                    compose_project="awf_ws_exact",
                    compose_file=compose,
                    exc=SimpleNamespace(reason_code="AGENT_IDLE_TIMEOUT"),
                    service_healthy=False,
                    restart_attempts=0,
                )
                == 1
            )
    assert calls(log)
    assert {call["host"] for call in calls(log)} == {EXECUTION}


@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
@pytest.mark.parametrize("denial", ["auth", "owner", "version"])
async def test_public_controls_denied_before_any_docker_command(
    owned_workspace, tmp_path, monkeypatch, docker_control_probe, action, denial
):
    log, _ = docker_control_probe
    async with running_app(owned_workspace, tmp_path, monkeypatch) as (client, _, _, headers):
        if denial == "auth":
            headers["Authorization"] = "Bearer synthetic-wrong-operator"
        elif denial == "owner":
            identity = owned_workspace[0].receiver.identity
            headers.update(identity.model_copy(update={"owner": "foreign/node"}).headers())
        else:
            headers["If-Match"] = "999999"
        response = await request_control(client, action, headers)
        assert response.status_code == {"auth": 401, "owner": 403, "version": 409}[denial]
    assert not calls(log)


@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
async def test_public_control_replay_does_not_clean_again(
    owned_workspace, tmp_path, monkeypatch, docker_control_probe, action
):
    log, _ = docker_control_probe
    async with running_app(owned_workspace, tmp_path, monkeypatch) as (client, _, _, headers):
        first = await request_control(client, action, headers)
        before = calls(log)
        replay = await request_control(client, action, headers)
        assert first.status_code == replay.status_code == 200
        assert first.json()["operation_id"] == replay.json()["operation_id"]
        assert before and calls(log) == before


@pytest.mark.parametrize("mode", ["cancel-no-stack", "hosted-pr", "remonitor-denied"])
async def test_no_command_control_paths_remain_no_command(
    owned_workspace, tmp_path, monkeypatch, docker_control_probe, mode
):
    log, _ = docker_control_probe
    async with running_app(owned_workspace, tmp_path, monkeypatch) as (client, _, factory, headers):
        if mode == "hosted-pr":
            async with factory.begin() as session:
                row = await session.get(Workspace, "ws_cloud_stable")
                row.task_policy = {
                    **row.task_policy,
                    "pr_adoption": {"execution": {"mode": "hosted"}},
                }
            response = await request_control(client, "destroy", headers)
            assert response.status_code == 200, response.text
            assert response.json()["operation_status"] == "succeeded"
        else:
            action = "cancel" if mode == "cancel-no-stack" else "remonitor"
            response = await client.post(
                "/v1/workspaces/ws_cloud_stable/" + action,
                json={"stop_stack": False},
                headers=headers,
            )
            assert response.status_code == (200 if mode == "cancel-no-stack" else 403)
    assert not calls(log)


async def test_launch_race_stop_and_failure_keep_execution_endpoint(
    owned_workspace, docker_control_probe
):
    from awf.db.repositories import TERMINAL_RUNTIME_RELEASE_REASON_CODE, WorkspaceRepository
    from awf.node.provisioner_launch_cleanup import _launch_lost_to_terminal_cleanup
    from awf.runtime.inspection import inspection_environment

    _, _, factory = owned_workspace
    log, mode = docker_control_probe
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.status = "destroyed"
        await WorkspaceRepository(session).add_event(
            row,
            event_type="workspace.terminal_runtime_released",
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            payload={},
        )
    environment = {"PATH": os.environ["PATH"], "DOCKER_HOST": EXECUTION}
    with inspection_environment(environment):
        mode.write_text("present")
        assert await _launch_lost_to_terminal_cleanup(
            SimpleNamespace(_session_factory=factory), "ws_cloud_stable"
        )
        assert [call["args"][0] for call in calls(log)] == ["ps", "stop"]
        mode.write_text("failure")
        assert await _launch_lost_to_terminal_cleanup(
            SimpleNamespace(_session_factory=factory), "ws_cloud_stable"
        )
    assert {call["host"] for call in calls(log)} == {EXECUTION}
    async with factory() as session:
        revoked = list(
            await session.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == "ws_cloud_stable",
                    WorkspaceEvent.event_type == "workspace.terminal_runtime_release_revoked",
                )
            )
        )
        assert len(revoked) == 1


async def test_local_defaults_custom_factories_and_explicit_roots_remain_supported(
    tmp_path, monkeypatch, docker_control_probe
):
    settings = Settings(work_dir=str(tmp_path / "explicit"))
    monkeypatch.setattr(controls_helpers, "get_settings", lambda: settings)
    cleaner = controls_helpers.default_cleaner()
    await cleaner.cleanup(
        workspace_id="ws_local",
        repo_url="https://example.invalid/repo",
        compose_project_name="awf_ws_local",
        remove_worktree=False,
    )
    await controls_helpers.stop_project_containers("awf_ws_local")
    assert {call["host"] for call in calls(docker_control_probe[0])} == {CONTROL}
    factory, stopper = lambda: cleaner, AsyncMock()
    service = WorkspaceService(
        None, settings=settings, cleaner_factory=factory, project_stopper=stopper
    )
    assert service._cleaner_factory is factory
    assert service._project_stopper is stopper


async def test_task_and_explicit_service_bindings_are_isolated(
    owned_workspace, tmp_path, docker_control_probe
):
    from awf.runtime.inspection import inspection_environment

    authority, _, factory = owned_workspace
    base = Settings(
        work_dir=str(tmp_path), hosted_node=authority.receiver.config, docker_host=EXECUTION
    )
    other = base.model_copy(update={"docker_host": "unix:///second-execution.sock"})
    services = [WorkspaceService(factory, settings=settings) for settings in (base, other)]

    async def cleanup(index):
        with inspection_environment(
            {"PATH": os.environ["PATH"], "DOCKER_HOST": "unix:///other-task.sock"}
        ):
            await (
                services[index]
                ._cleaner_factory()
                .cleanup(
                    workspace_id=f"ws_{index}",
                    repo_url="https://example.invalid/repo",
                    compose_project_name=f"awf_ws_{index}",
                    remove_worktree=False,
                )
            )

    before = dict(os.environ)
    await asyncio.gather(cleanup(0), cleanup(1))
    observed = calls(docker_control_probe[0])
    assert {call["host"] for call in observed} == {EXECUTION, other.docker_host}
    for call in observed:
        expected = (
            EXECUTION
            if "label=com.docker.compose.project=awf_ws_0" in call["args"]
            else other.docker_host
        )
        assert call["host"] == expected
        assert call["context"] is None and not call["inherited_secret"]
    assert dict(os.environ) == before


@pytest.mark.parametrize("endpoint", ["control", "tcp://untrusted:2375"])
def test_hosted_configuration_never_falls_back_to_control_endpoint(
    owned_workspace, tmp_path, docker_control_probe, endpoint
):
    from awf.service.controls_errors import WorkspaceStackStopError

    config = owned_workspace[0].receiver.config
    host = f"unix://{config.host.docker_socket}" if endpoint == "control" else endpoint
    settings = Settings(hosted_node=config, docker_host=host, work_dir=str(tmp_path))
    with pytest.raises(WorkspaceStackStopError, match="EXECUTION_DOCKER_ENDPOINT_REQUIRED"):
        controls_helpers.default_cleaner(settings=settings)
    assert not calls(docker_control_probe[0])
