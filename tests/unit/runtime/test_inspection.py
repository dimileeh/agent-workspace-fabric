"""Runtime inspection edge cases."""

from __future__ import annotations

import json
import sys

import pytest

from awf.runtime import inspection
from awf.runtime.inspection import RuntimeInspector, RuntimeService, RuntimeSnapshot, _ProcessResult


@pytest.mark.unit
async def test_run_captures_successful_subprocess_output() -> None:
    result = await inspection._run(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"]
    )

    assert result.returncode == 0
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


@pytest.mark.unit
async def test_runtime_inspector_returns_unavailable_when_docker_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _missing_exec(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(inspection.asyncio, "create_subprocess_exec", _missing_exec)

    snapshot = await RuntimeInspector().inspect("awf_ws_missing_cli")

    assert snapshot.stack_state == "unavailable"
    assert snapshot.services == []
    assert snapshot.reason is not None
    assert "docker executable is not available" in snapshot.reason


@pytest.mark.unit
async def test_runtime_inspector_reports_missing_compose_project() -> None:
    snapshot = await RuntimeInspector().inspect(None)

    assert snapshot.stack_state == "unknown"
    assert snapshot.services == []
    assert snapshot.reason == "workspace has no compose project"


@pytest.mark.unit
async def test_runtime_inspector_reports_docker_ps_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run(args: list[str]) -> _ProcessResult:
        assert args[:2] == ["docker", "ps"]
        return _ProcessResult(returncode=1, stdout="stdout detail", stderr="daemon down")

    monkeypatch.setattr(inspection, "_run", _fake_run)

    snapshot = await RuntimeInspector().inspect("awf_ws_down")

    assert snapshot.stack_state == "unavailable"
    assert snapshot.services == []
    assert snapshot.reason == "daemon down"


@pytest.mark.unit
async def test_runtime_inspector_uses_stdout_when_docker_ps_has_no_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run(args: list[str]) -> _ProcessResult:
        assert args[:2] == ["docker", "ps"]
        return _ProcessResult(returncode=1, stdout="cannot connect", stderr="")

    monkeypatch.setattr(inspection, "_run", _fake_run)

    snapshot = await RuntimeInspector().inspect("awf_ws_down")

    assert snapshot.stack_state == "unavailable"
    assert snapshot.reason == "cannot connect"


@pytest.mark.unit
async def test_runtime_inspector_skips_blank_and_malformed_ps_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run(args: list[str]) -> _ProcessResult:
        assert args[:2] == ["docker", "ps"]
        return _ProcessResult(returncode=0, stdout="\nnot json\n", stderr="")

    monkeypatch.setattr(inspection, "_run", _fake_run)

    snapshot = await RuntimeInspector().inspect("awf_ws_empty")

    assert snapshot.stack_state == "stopped"
    assert snapshot.services == []


@pytest.mark.unit
async def test_runtime_inspector_builds_running_service_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ps_row = {
        "ID": "abc123",
        "Image": "awf-agent-runtime:latest",
        "Names": "awf_ws_agent_1",
        "State": "Up",
        "Status": "Up 2 minutes",
        "Ports": "0.0.0.0:3000->3000/tcp, 127.0.0.1:9222->9222/tcp",
    }

    async def _fake_run(args: list[str]) -> _ProcessResult:
        assert args[:2] == ["docker", "ps"]
        return _ProcessResult(returncode=0, stdout=json.dumps(ps_row) + "\n", stderr="")

    async def _fake_inspect(container_id: str) -> dict[str, object]:
        assert container_id == "abc123"
        return {
            "Config": {"Labels": {"com.docker.compose.service": "agent"}},
            "State": {
                "Status": "running",
                "StartedAt": "2026-04-27T11:00:00Z",
                "Health": {"Status": "healthy"},
            },
        }

    monkeypatch.setattr(inspection, "_run", _fake_run)
    monkeypatch.setattr(inspection, "_inspect_container", _fake_inspect)

    snapshot = await RuntimeInspector().inspect("awf_ws_running")

    assert snapshot.stack_state == "running"
    assert len(snapshot.services) == 1
    service = snapshot.services[0]
    assert service.name == "agent"
    assert service.container_id == "abc123"
    assert service.image == "awf-agent-runtime:latest"
    assert service.state == "running"
    assert service.status == "Up 2 minutes"
    assert service.health == "healthy"
    assert service.started_at == "2026-04-27T11:00:00Z"
    assert service.ports == ["0.0.0.0:3000->3000/tcp", "127.0.0.1:9222->9222/tcp"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "inspect_data",
    [
        {},
        {"Config": {"Labels": {}}, "State": {"Status": "running"}},
    ],
)
async def test_probe_agent_service_health_is_indeterminate_when_running_labels_missing(
    monkeypatch: pytest.MonkeyPatch,
    inspect_data: dict[str, object],
) -> None:
    ps_row = {
        "ID": "abc123",
        "Image": "awf-agent-runtime:latest",
        "Names": "awf_ws_agent_1",
        "State": "running",
        "Status": "Up 2 minutes",
    }

    async def _fake_run(args: list[str]) -> _ProcessResult:
        assert args[:2] == ["docker", "ps"]
        return _ProcessResult(returncode=0, stdout=json.dumps(ps_row) + "\n", stderr="")

    async def _fake_inspect(container_id: str) -> dict[str, object]:
        assert container_id == "abc123"
        return inspect_data

    monkeypatch.setattr(inspection, "_run", _fake_run)
    monkeypatch.setattr(inspection, "_inspect_container", _fake_inspect)

    assert await inspection.probe_agent_service_health(RuntimeInspector(), "awf_ws_running") is None


@pytest.mark.unit
async def test_runtime_inspector_uses_row_fallbacks_for_stopped_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ps_rows = [
        {"ID": "one", "Image": "redis:7", "Names": "awf_ws_redis", "State": "Exited"},
        {"Image": "orphan", "State": None},
    ]

    async def _fake_run(args: list[str]) -> _ProcessResult:
        assert args[:2] == ["docker", "ps"]
        return _ProcessResult(
            returncode=0,
            stdout="\n".join(json.dumps(row) for row in ps_rows),
            stderr="",
        )

    async def _fake_inspect(container_id: str) -> dict[str, object]:
        assert container_id == "one"
        return {"Config": {"Labels": {}}, "State": {"Status": ""}}

    monkeypatch.setattr(inspection, "_run", _fake_run)
    monkeypatch.setattr(inspection, "_inspect_container", _fake_inspect)

    snapshot = await RuntimeInspector().inspect("awf_ws_stopped")

    assert snapshot.stack_state == "stopped"
    assert [service.name for service in snapshot.services] == ["awf_ws_redis", "unknown"]
    assert [service.state for service in snapshot.services] == ["exited", "unknown"]
    assert snapshot.services[1].container_id is None


@pytest.mark.unit
async def test_inspect_container_returns_first_dict_from_docker_inspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run(args: list[str]) -> _ProcessResult:
        assert args == ["docker", "inspect", "abc123"]
        return _ProcessResult(returncode=0, stdout='[{"State": {"Status": "running"}}]', stderr="")

    monkeypatch.setattr(inspection, "_run", _fake_run)

    assert await inspection._inspect_container("abc123") == {"State": {"Status": "running"}}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_ProcessResult(returncode=1, stdout="[]", stderr="missing"), {}),
        (_ProcessResult(returncode=0, stdout="not json", stderr=""), {}),
        (_ProcessResult(returncode=0, stdout="[]", stderr=""), {}),
        (_ProcessResult(returncode=0, stdout='["not a dict"]', stderr=""), {}),
    ],
)
async def test_inspect_container_returns_empty_dict_for_unusable_inspect_output(
    monkeypatch: pytest.MonkeyPatch,
    result: _ProcessResult,
    expected: dict[str, object],
) -> None:
    async def _fake_run(args: list[str]) -> _ProcessResult:
        assert args == ["docker", "inspect", "abc123"]
        return result

    monkeypatch.setattr(inspection, "_run", _fake_run)

    assert await inspection._inspect_container("abc123") == expected


@pytest.mark.unit
def test_runtime_helper_fallbacks() -> None:
    assert (
        inspection._service_name(
            {"ID": "abc123"},
            {"Config": {"Labels": {"com.docker.compose.service": "web"}}},
        )
        == "web"
    )
    assert inspection._service_name({"ID": "abc123"}, {"Config": {"Labels": []}}) == "abc123"
    assert inspection._service_name({}, {}) == "unknown"
    assert (
        inspection._command_from({}, {"Config": {"Cmd": ["sleep", "infinity"]}}) == "sleep infinity"
    )
    assert inspection._command_from({}, {"Config": {"Cmd": ["sleep", None, "10"]}}) == "sleep 10"
    assert inspection._command_from({}, {"Config": {"Cmd": [None]}}) is None
    assert inspection._command_from({}, {"Config": {"Cmd": "python -m app"}}) == "python -m app"
    assert (
        inspection._command_from({"Command": "echo hi"}, {"Config": {"Cmd": ["sleep"]}})
        == "echo hi"
    )
    assert inspection._command_from({}, {"Config": {"Cmd": None}}) is None
    assert inspection._state_from({"State": "Exited"}, {}) == "exited"
    assert inspection._state_from({}, {}) == "unknown"
    assert inspection._ports_from({"Ports": ""}) == []
    assert inspection._ports_from({"Ports": None}) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "service",
    [
        RuntimeService(name="agent", container_id="abc", image="agent", state="running"),
        RuntimeService(
            name="agent",
            container_id="abc",
            image="agent",
            state="running",
            health="healthy",
        ),
    ],
)
def test_agent_service_health_from_snapshot_running(service: RuntimeService) -> None:
    snapshot = RuntimeSnapshot(stack_state="running", services=[service])

    assert inspection.agent_service_health_from_snapshot(snapshot) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "service",
    [
        RuntimeService(name="agent", container_id="abc", image="agent", state="exited"),
        RuntimeService(name="agent", container_id="abc", image="agent", state="dead"),
        RuntimeService(
            name="agent",
            container_id="abc",
            image="agent",
            state="running",
            health="starting",
        ),
        RuntimeService(
            name="agent",
            container_id="abc",
            image="agent",
            state="running",
            health="unhealthy",
        ),
    ],
)
def test_agent_service_health_from_snapshot_down(service: RuntimeService) -> None:
    snapshot = RuntimeSnapshot(stack_state="running", services=[service])

    assert inspection.agent_service_health_from_snapshot(snapshot) is False


@pytest.mark.unit
def test_agent_service_health_from_snapshot_missing_agent_is_down() -> None:
    snapshot = RuntimeSnapshot(
        stack_state="running",
        services=[
            RuntimeService(
                name="postgres",
                container_id="pg",
                image="postgres",
                state="running",
            )
        ],
    )

    assert inspection.agent_service_health_from_snapshot(snapshot) is False


@pytest.mark.unit
@pytest.mark.parametrize("stack_state", ["unknown", "unavailable"])
def test_agent_service_health_from_snapshot_indeterminate_stack(stack_state: str) -> None:
    snapshot = RuntimeSnapshot(stack_state=stack_state, reason="docker unavailable")

    assert inspection.agent_service_health_from_snapshot(snapshot) is None


@pytest.mark.unit
async def test_probe_agent_service_health_returns_none_on_probe_exception() -> None:
    class _BrokenInspector:
        async def inspect(self, _compose_project_name: str) -> RuntimeSnapshot:
            raise RuntimeError("docker exploded")

    assert await inspection.probe_agent_service_health(_BrokenInspector(), "awf_ws") is None
