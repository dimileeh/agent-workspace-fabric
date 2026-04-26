"""Service status disk-pressure and orphan-container checks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from awf.service.config import ServiceSettings
from awf.service.status import WorkspaceIdView, collect_service_status


def _settings(tmp_path: Path, *, min_free_disk_bytes: int = 200) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="sqlite+aiosqlite:///:memory:",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        min_free_disk_bytes=min_free_disk_bytes,
    )


class _Response:
    status_code = 200

    def json(self) -> dict[str, str]:
        return {"status": "ok"}

    def raise_for_status(self) -> None:
        return None


class _DiskUsage:
    def __init__(self, *, total: int, used: int, free: int) -> None:
        self.total = total
        self.used = used
        self.free = free


async def _api_get(_url: str, *, timeout: float) -> _Response:
    return _Response()


async def _db_probe(_database_url: str) -> dict[str, Any]:
    return {"ok": True, "status": "ok"}


def _docker_ps_payload(*containers: dict[str, str]) -> str:
    return "".join(json.dumps(container) + "\n" for container in containers)


def _make_run_subprocess(
    *,
    ps_payload: str = "",
    ps_returncode: int = 0,
    ps_stderr: str = "",
) -> Any:
    def _run(args: list[str], **_kwargs: object) -> Any:
        if args[:2] == ["docker", "info"]:
            return type("Completed", (), {"returncode": 0, "stdout": "27.0.3\n", "stderr": ""})()
        if args[:3] == ["docker", "image", "inspect"]:
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "sha256:deadbeef\n", "stderr": ""},
            )()
        if args[:3] == ["docker", "ps", "-a"]:
            return type(
                "Completed",
                (),
                {"returncode": ps_returncode, "stdout": ps_payload, "stderr": ps_stderr},
            )()
        raise AssertionError(f"unexpected subprocess call: {args}")

    return _run


async def _empty_workspace_view(_database_url: str) -> WorkspaceIdView:
    return WorkspaceIdView(
        active_ids=frozenset(),
        terminal_ids=frozenset(),
        available=True,
    )


@pytest.mark.unit
def test_service_status_includes_ok_disk_check_from_mocked_usage(tmp_path: Path) -> None:
    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path, min_free_disk_bytes=200),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
        )
    )

    assert status["status"] == "ok"
    disk = status["checks"]["disk"]
    assert disk["ok"] is True
    assert disk["status"] == "ok"
    assert disk["reason"] == "SUFFICIENT_DISK"
    assert disk["total_bytes"] == 1000
    assert disk["used_bytes"] == 700
    assert disk["free_bytes"] == 300
    assert disk["percent_free"] == 30.0
    assert disk["threshold_bytes"] == 200


@pytest.mark.unit
def test_service_status_fails_when_disk_is_below_threshold(tmp_path: Path) -> None:
    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path, min_free_disk_bytes=400),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
        )
    )

    assert status["status"] == "fail"
    disk = status["checks"]["disk"]
    assert disk["ok"] is False
    assert disk["status"] == "fail"
    assert disk["reason"] == "INSUFFICIENT_DISK"
    assert disk["threshold_bytes"] == 400
    assert "AWF_MIN_FREE_DISK_BYTES" in str(disk["detail"])


@pytest.mark.unit
def test_orphan_check_reports_no_orphans_when_no_awf_containers(tmp_path: Path) -> None:
    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=""),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
        )
    )

    assert status["status"] == "ok"
    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is True
    assert orphans["status"] == "ok"
    assert orphans["reason"] == "NO_ORPHANS"
    assert orphans["orphan_count"] == 0
    assert orphans["active_count"] == 0
    assert orphans["examples"] == []


@pytest.mark.unit
def test_orphan_check_treats_active_workspace_containers_as_expected(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        {
            "ID": "abc",
            "Names": "awf_ws_alive-agent-1",
            "State": "running",
            "Status": "Up 3 minutes",
            "Labels": (
                "com.docker.compose.project=awf_ws_alive,com.docker.compose.service=agent"
            ),
        },
        {
            "ID": "def",
            "Names": "awf_ws_alive-postgres-1",
            "State": "running",
            "Status": "Up 3 minutes (healthy)",
            "Labels": (
                "com.docker.compose.project=awf_ws_alive,com.docker.compose.service=postgres"
            ),
        },
    )

    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset({"ws_alive"}),
            terminal_ids=frozenset(),
            available=True,
        )

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
        )
    )

    assert status["status"] == "ok"
    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["reason"] == "NO_ORPHANS"
    assert orphans["active_count"] == 1
    assert orphans["orphan_count"] == 0


@pytest.mark.unit
def test_orphan_check_flags_terminal_workspace_with_running_container(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        {
            "ID": "abc",
            "Names": "awf_ws_dead-agent-1",
            "State": "running",
            "Status": "Up 1 day",
            "Labels": (
                "com.docker.compose.project=awf_ws_dead,com.docker.compose.service=agent"
            ),
        }
    )

    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset({"ws_dead"}),
            available=True,
        )

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
        )
    )

    assert status["status"] == "fail"
    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is False
    assert orphans["status"] == "fail"
    assert orphans["reason"] == "ORPHANS_PRESENT"
    assert orphans["orphan_count"] == 1
    assert orphans["active_count"] == 0
    examples = orphans["examples"]
    assert isinstance(examples, list)
    assert len(examples) == 1
    example = examples[0]
    assert example["workspace_id"] == "ws_dead"
    assert example["compose_project"] == "awf_ws_dead"
    assert example["classification"] == "terminal"
    assert example["reason"] == "WORKSPACE_TERMINAL"
    action = orphans.get("action")
    assert isinstance(action, str) and action.strip()


@pytest.mark.unit
def test_orphan_check_flags_workspace_missing_from_db(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        {
            "ID": "ghost",
            "Names": "awf-ws_ghost-agent-1",
            "State": "exited",
            "Status": "Exited (0) 5 minutes ago",
            "Labels": (
                "com.docker.compose.project=awf-ws_ghost,com.docker.compose.service=agent"
            ),
        }
    )

    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset({"ws_alive"}),
            terminal_ids=frozenset(),
            available=True,
        )

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is False
    assert orphans["reason"] == "ORPHANS_PRESENT"
    examples = orphans["examples"]
    assert examples[0]["workspace_id"] == "ws_ghost"
    assert examples[0]["compose_project"] == "awf-ws_ghost"
    assert examples[0]["classification"] == "missing"
    assert examples[0]["reason"] == "WORKSPACE_MISSING"


@pytest.mark.unit
def test_orphan_check_skips_non_awf_compose_projects(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        {
            "ID": "x",
            "Names": "myapp-db-1",
            "State": "running",
            "Status": "Up",
            "Labels": "com.docker.compose.project=myapp,com.docker.compose.service=db",
        }
    )

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is True
    assert orphans["reason"] == "NO_ORPHANS"
    assert orphans["orphan_count"] == 0


@pytest.mark.unit
def test_orphan_check_marks_unknown_when_db_unavailable(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        {
            "ID": "abc",
            "Names": "awf_ws_solo-agent-1",
            "State": "running",
            "Status": "Up 2 minutes",
            "Labels": (
                "com.docker.compose.project=awf_ws_solo,com.docker.compose.service=agent"
            ),
        }
    )

    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset(),
            available=False,
        )

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is True
    assert orphans["status"] == "unknown"
    assert orphans["reason"] == "DB_UNAVAILABLE"
    assert orphans["container_count"] == 1
    examples = orphans["examples"]
    assert isinstance(examples, list)
    assert examples[0]["workspace_id"] == "ws_solo"
    assert examples[0]["compose_project"] == "awf_ws_solo"
    assert examples[0]["classification"] == "unknown"


@pytest.mark.unit
def test_orphan_check_unavailable_when_docker_ps_fails(tmp_path: Path) -> None:
    def _run(args: list[str], **_kwargs: object) -> Any:
        if args[:2] == ["docker", "info"]:
            return type("Completed", (), {"returncode": 0, "stdout": "27\n", "stderr": ""})()
        if args[:3] == ["docker", "image", "inspect"]:
            return type("Completed", (), {"returncode": 0, "stdout": "sha\n", "stderr": ""})()
        if args[:3] == ["docker", "ps", "-a"]:
            return type(
                "Completed",
                (),
                {"returncode": 1, "stdout": "", "stderr": "Cannot connect to Docker"},
            )()
        raise AssertionError(f"unexpected subprocess call: {args}")

    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset({"ws_alive"}),
            terminal_ids=frozenset(),
            available=True,
        )

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run,
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is True
    assert orphans["status"] == "unavailable"
    assert orphans["reason"] == "DOCKER_UNAVAILABLE"
    assert "Cannot connect" in str(orphans.get("detail", ""))
    assert orphans["orphan_count"] == 0


@pytest.mark.unit
def test_orphan_check_handles_missing_docker_binary(tmp_path: Path) -> None:
    def _run(args: list[str], **_kwargs: object) -> Any:
        raise FileNotFoundError("docker")

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run,
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is True
    assert orphans["status"] == "unavailable"
    assert orphans["reason"] == "DOCKER_CLI_NOT_FOUND"
