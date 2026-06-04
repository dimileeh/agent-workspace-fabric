"""Service status disk-pressure and orphan-container checks."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import InterfaceError

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.service import status as status_mod
from awf.service.config import ServiceSettings
from awf.service.status import (
    WorkspaceIdView,
    WorkspaceLifecycleSnapshot,
    _check_agent_runtime_image,
    _check_api,
    _check_docker,
    _default_workspace_id_lookup,
    _docker_result_to_check,
    _docker_socket_path,
    _fail,
    _http_get,
    _run_docker_command,
    _run_subprocess,
    _truncate,
    _workspace_id_from_project,
    check_database,
    collect_service_status,
)
from tests.postgres import postgres_test_url

_POSTGRES_TEST_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"


def _closed_connection_error() -> InterfaceError:
    return InterfaceError("SELECT 1", {}, RuntimeError("connection is closed"))


def _settings(
    tmp_path: Path,
    *,
    min_free_disk_bytes: int = 200,
    database_url: str = _POSTGRES_TEST_URL,
    completed_workspace_retention_hours: float = 168,
) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url=database_url,
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
        min_free_disk_bytes=min_free_disk_bytes,
        completed_workspace_retention_hours=completed_workspace_retention_hours,
    )


class _Response:
    status_code = 200

    def json(self) -> dict[str, str]:
        return {"status": "ok"}

    def raise_for_status(self) -> None:
        return None


class _JsonErrorResponse:
    status_code = 200

    def json(self) -> dict[str, str]:
        raise ValueError("not json")

    def raise_for_status(self) -> None:
        return None


class _VersionResponse:
    status_code = 200

    def json(self) -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    def raise_for_status(self) -> None:
        return None


class _ListResponse:
    status_code = 200

    def json(self) -> list[str]:
        return ["ok"]

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


def _container(
    *,
    id: str,
    name: str,
    state: str,
    status: str,
    project: str,
    service: str,
) -> dict[str, str]:
    return {
        "id": id,
        "name": name,
        "state": state,
        "status": status,
        "project": project,
        "service": service,
    }


def _make_run_subprocess(
    *,
    ps_payload: str = "",
    ps_returncode: int = 0,
    ps_stderr: str = "",
    network_payload: str = "",
    network_returncode: int = 0,
    network_stderr: str = "",
    volume_payload: str = "",
    volume_returncode: int = 0,
    volume_stderr: str = "",
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
        if args[:3] == ["docker", "network", "ls"]:
            return type(
                "Completed",
                (),
                {
                    "returncode": network_returncode,
                    "stdout": network_payload,
                    "stderr": network_stderr,
                },
            )()
        if args[:3] == ["docker", "volume", "ls"]:
            return type(
                "Completed",
                (),
                {
                    "returncode": volume_returncode,
                    "stdout": volume_payload,
                    "stderr": volume_stderr,
                },
            )()
        raise AssertionError(f"unexpected subprocess call: {args}")

    return _run


async def _empty_workspace_view(_database_url: str) -> WorkspaceIdView:
    return WorkspaceIdView(
        active_ids=frozenset(),
        terminal_ids=frozenset(),
        available=True,
    )


async def _seed_cleanup_status_workspace(
    database_url: str,
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    title: str,
    pr: bool = False,
) -> str:
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        if pr:
            workspace.pr_url = "https://github.com/example/repo/pull/42"
            workspace.pr_number = 42
            workspace.pr_merge_sha = "e" * 40
        await session.commit()
        workspace_id = workspace.id
    await engine.dispose()
    return workspace_id


@pytest.mark.unit
def test_orphan_check_marks_unknown_when_db_unavailable(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        _container(
            id="abc",
            name="awf_ws_solo-agent-1",
            state="running",
            status="Up 2 minutes",
            project="awf_ws_solo",
            service="agent",
        )
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
            provider_environ={},
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
            provider_environ={},
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is True
    assert orphans["status"] == "unavailable"
    assert orphans["reason"] == "DOCKER_UNAVAILABLE"
    assert "Cannot connect" in str(orphans.get("detail", ""))
    assert orphans["orphan_count"] == 0


@pytest.mark.unit
def test_collect_status_cancels_pending_auxiliary_tasks_on_probe_error(tmp_path: Path) -> None:
    lookup_cancelled = False

    async def failing_db_probe(_database_url: str) -> dict[str, Any]:
        raise RuntimeError("db probe exploded")

    async def slow_workspace_lookup(_database_url: str) -> WorkspaceIdView:
        nonlocal lookup_cancelled
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            lookup_cancelled = True
            raise
        raise AssertionError("workspace lookup should have been cancelled")

    with pytest.raises(RuntimeError, match="db probe exploded"):
        asyncio.run(
            collect_service_status(
                _settings(tmp_path),
                api_get=_api_get,
                db_probe=failing_db_probe,
                run_subprocess=_make_run_subprocess(),
                socket_exists=lambda _path: True,
                disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
                workspace_id_lookup=slow_workspace_lookup,
                provider_environ={},
            )
        )

    assert lookup_cancelled is True


@pytest.mark.unit
def test_collect_status_cleanup_does_not_suppress_base_exceptions(tmp_path: Path) -> None:
    class FatalCleanup(BaseException):
        pass

    async def failing_db_probe(_database_url: str) -> dict[str, Any]:
        raise RuntimeError("db probe exploded")

    async def fatal_workspace_lookup(_database_url: str) -> WorkspaceIdView:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError as exc:
            raise FatalCleanup("cleanup should escape") from exc
        raise AssertionError("workspace lookup should have been cancelled")

    with pytest.raises(FatalCleanup, match="cleanup should escape"):
        asyncio.run(
            collect_service_status(
                _settings(tmp_path),
                api_get=_api_get,
                db_probe=failing_db_probe,
                run_subprocess=_make_run_subprocess(),
                socket_exists=lambda _path: True,
                disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
                workspace_id_lookup=fatal_workspace_lookup,
                provider_environ={},
            )
        )


@pytest.mark.unit
def test_orphan_check_extracts_labels_via_docker_template(tmp_path: Path) -> None:
    captured_args: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> Any:
        captured_args.append(list(args))
        if args[:2] == ["docker", "info"]:
            return type("Completed", (), {"returncode": 0, "stdout": "27\n", "stderr": ""})()
        if args[:3] == ["docker", "image", "inspect"]:
            return type("Completed", (), {"returncode": 0, "stdout": "sha\n", "stderr": ""})()
        if args[:3] == ["docker", "ps", "-a"]:
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if args[:3] == ["docker", "network", "ls"]:
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if args[:3] == ["docker", "volume", "ls"]:
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        raise AssertionError(f"unexpected subprocess call: {args}")

    asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run,
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            provider_environ={},
        )
    )

    ps_args = next(args for args in captured_args if args[:3] == ["docker", "ps", "-a"])
    fmt_index = ps_args.index("--format")
    fmt = ps_args[fmt_index + 1]
    assert '.Label "com.docker.compose.project"' in fmt
    assert '.Label "com.docker.compose.service"' in fmt
    network_args = next(args for args in captured_args if args[:3] == ["docker", "network", "ls"])
    assert "label=com.docker.compose.project" in network_args
    volume_args = next(args for args in captured_args if args[:3] == ["docker", "volume", "ls"])
    assert "label=com.docker.compose.project" in volume_args


@pytest.mark.unit
def test_service_status_treats_completed_within_retention_salvage_as_retained(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    (Path(settings.work_dir) / "git" / "worktrees" / "ws_done").mkdir(parents=True)
    volume_payload = _docker_ps_payload(
        {"name": "awf_ws_done_postgres_data", "project": "awf_ws_done"}
    )

    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset({"ws_done"}),
            available=True,
            snapshots=(
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_done",
                    status=WorkspaceStatus.completed.value,
                    updated_at=datetime.now(UTC),
                    compose_project_name="awf_ws_done",
                ),
            ),
        )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(volume_payload=volume_payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
            provider_environ={},
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert status["status"] == "ok"
    assert orphans["reason"] == "NO_ORPHANS"
    assert orphans["retained_count"] == 2
    assert orphans["orphan_count"] == 0


@pytest.mark.unit
def test_service_status_flags_completed_within_retention_live_runtime_as_leak(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    payload = _docker_ps_payload(
        _container(
            id="abc",
            name="awf_ws_zombie-agent-1",
            state="running",
            status="Up 5 minutes",
            project="awf_ws_zombie",
            service="agent",
        )
    )
    network_payload = _docker_ps_payload(
        {"id": "net", "name": "awf_ws_zombie_default", "project": "awf_ws_zombie"}
    )

    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset({"ws_zombie"}),
            available=True,
            snapshots=(
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_zombie",
                    status=WorkspaceStatus.completed.value,
                    updated_at=datetime.now(UTC),
                    compose_project_name="awf_ws_zombie",
                ),
            ),
        )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(
                ps_payload=payload,
                network_payload=network_payload,
            ),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
            provider_environ={},
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is False
    assert orphans["reason"] == "ORPHANS_PRESENT"
    assert orphans["orphan_count"] == 2
    assert orphans["orphan_counts_by_kind"] == {"container": 1, "network": 1}
    reasons = {example["reason"] for example in orphans["examples"]}
    assert reasons == {"WORKSPACE_TERMINAL_LIVE_RUNTIME"}


@pytest.mark.unit
def test_orphan_check_handles_label_value_with_comma(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        _container(
            id="abc",
            name="awf_ws_alive-agent-1",
            state="running",
            status="Up 3 minutes, restarting",
            project="awf_ws_alive",
            service="agent",
        )
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
            provider_environ={},
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["reason"] == "NO_ORPHANS"
    assert orphans["active_count"] == 1


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
            provider_environ={},
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is True
    assert orphans["status"] == "unavailable"
    assert orphans["reason"] == "DOCKER_CLI_NOT_FOUND"


@pytest.mark.unit
def test_default_workspace_id_lookup_returns_unavailable_for_malformed_url() -> None:
    view = asyncio.run(_default_workspace_id_lookup("not-a-real-database-url"))

    assert view.available is False
    assert view.active_ids == frozenset()
    assert view.terminal_ids == frozenset()


@pytest.mark.unit
async def test_database_probe_reports_closed_connection_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingConnection:
        async def __aenter__(self) -> object:
            raise _closed_connection_error()

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            return None

    class _FailingEngine:
        def connect(self) -> _FailingConnection:
            return _FailingConnection()

        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(status_mod, "make_engine", lambda _database_url: _FailingEngine())

    result = await check_database(_POSTGRES_TEST_URL)

    assert result["ok"] is False
    assert result["reason"] == "DB_CONNECTION_CLOSED"
    assert "connection is closed" in str(result["detail"])


@pytest.mark.unit
async def test_database_probe_and_workspace_lookup_read_postgres_rows(tmp_path: Path) -> None:
    async with postgres_test_url() as database_url:
        engine = make_engine(database_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as session:
                repo = WorkspaceRepository(session)
                await repo.create(
                    repo_url="git@github.com:example/active.git",
                    branch_base="main",
                    task_title="Active",
                    task_prompt="Active workspace",
                    agent="codex",
                    test_commands=["pytest -q"],
                )
                terminal = await repo.create(
                    repo_url="git@github.com:example/terminal.git",
                    branch_base="main",
                    task_title="Terminal",
                    task_prompt="Terminal workspace",
                    agent="codex",
                    test_commands=["pytest -q"],
                )
                terminal.status = WorkspaceStatus.completed.value
                ignored = await repo.create(
                    repo_url="git@github.com:example/ignored.git",
                    branch_base="main",
                    task_title="Ignored",
                    task_prompt="Unknown status workspace",
                    agent="codex",
                    test_commands=["pytest -q"],
                )
                ignored.status = "unknown_future_status"
                await session.commit()

            db_check = await check_database(database_url)
            view = await _default_workspace_id_lookup(database_url)
            failed = await check_database("postgresql+asyncpg://awf:awf_dev@127.0.0.1:1/awf")
        finally:
            await engine.dispose()

    assert db_check == {"ok": True, "status": "ok"}
    assert view.available is True
    assert len(view.active_ids) == 1
    assert len(view.terminal_ids) == 1
    assert view.active_ids.isdisjoint(view.terminal_ids)
    assert failed["ok"] is False
    assert failed["reason"] == "DB_CONNECTION_FAILED"


@pytest.mark.unit
def test_status_workspace_lookup_ignores_unknown_status_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed = False

    class _Rows:
        def all(self) -> list[tuple[str, str, object, object, object, object, object, object]]:
            return [
                ("ws_active", "running", None, None, None, None, None, None),
                ("ws_done", "completed", None, None, None, None, None, None),
                ("ws_future", "future_status", None, None, None, None, None, None),
            ]

    class _Connection:
        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> _Rows:
            return _Rows()

    class _Engine:
        def connect(self) -> _Connection:
            return _Connection()

        async def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    monkeypatch.setattr(status_mod, "make_engine", lambda _url: _Engine())

    view = asyncio.run(_default_workspace_id_lookup(_POSTGRES_TEST_URL))

    assert view.active_ids == frozenset({"ws_active"})
    assert view.terminal_ids == frozenset({"ws_done"})
    assert view.snapshots[-1].workspace_id == "ws_future"
    assert disposed is True


@pytest.mark.unit
def test_status_helpers_cover_api_json_and_failure_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    async def bad_json_get(_url: str, *, timeout: float) -> _JsonErrorResponse:
        assert timeout == 5.0
        return _JsonErrorResponse()

    async def version_get(_url: str, *, timeout: float) -> _VersionResponse:
        assert _url == "http://localhost:8000/healthz"
        return _VersionResponse()

    async def list_get(_url: str, *, timeout: float) -> _ListResponse:
        return _ListResponse()

    async def raising_get(_url: str, *, timeout: float) -> _Response:
        raise RuntimeError("api down")

    assert asyncio.run(_check_api(settings, bad_json_get)) == {"ok": True, "status": "ok"}
    assert asyncio.run(_check_api(settings, list_get)) == {"ok": True, "status": "ok"}
    assert asyncio.run(_check_api(settings, version_get)) == {
        "ok": True,
        "status": "ok",
        "version": "0.1.0",
    }
    failed = asyncio.run(_check_api(settings, raising_get))
    assert failed["ok"] is False
    assert failed["reason"] == "API_UNREACHABLE"
    assert "RuntimeError" in str(failed["detail"])


@pytest.mark.unit
def test_status_helpers_cover_docker_socket_and_result_failures(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    missing_socket = _check_docker(
        settings,
        run_subprocess=_make_run_subprocess(),
        socket_exists=lambda _path: False,
    )
    assert missing_socket["reason"] == "DOCKER_SOCKET_UNREACHABLE"

    non_unix_settings = ServiceSettings(**{**settings.__dict__, "docker_host": "tcp://docker:2375"})
    docker_ok = _check_docker(
        non_unix_settings,
        run_subprocess=_make_run_subprocess(),
        socket_exists=lambda _path: False,
    )
    assert docker_ok["ok"] is True
    assert docker_ok["version"] == "27.0.3"
    image_ok = _check_agent_runtime_image(non_unix_settings, _make_run_subprocess())
    assert image_ok["version"] == "sha256:deadbeef"

    timeout_check = _docker_result_to_check(
        subprocess.TimeoutExpired(["docker", "info"], timeout=5),
        fail_reason="DOCKER_TIMEOUT",
        detail_prefix="docker: ",
    )
    assert timeout_check["reason"] == "DOCKER_TIMEOUT"
    generic_check = _docker_result_to_check(
        RuntimeError("boom"),
        fail_reason="DOCKER_FAILED",
        detail_prefix="docker: ",
    )
    assert generic_check["reason"] == "DOCKER_FAILED"
    failed_process = type("Completed", (), {"returncode": 1, "stdout": "stdout", "stderr": ""})()
    assert _docker_result_to_check(failed_process, fail_reason="DOCKER_FAILED") == {
        "ok": False,
        "status": "fail",
        "reason": "DOCKER_FAILED",
        "detail": "stdout",
    }


@pytest.mark.unit
def test_run_docker_command_passes_docker_host_and_captures_exceptions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    calls: list[dict[str, object]] = []

    def run(args: list[str], **kwargs: object) -> Any:
        calls.append({"args": args, **kwargs})
        return type("Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    result = _run_docker_command(["docker", "info"], settings=settings, run_subprocess=run)

    assert result.returncode == 0
    assert calls[0]["args"] == ["docker", "info"]
    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == settings.docker_host

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("docker exploded")

    captured = _run_docker_command(["docker", "info"], settings=settings, run_subprocess=boom)
    assert isinstance(captured, RuntimeError)


@pytest.mark.unit
async def test_docker_process_reports_missing_binary_without_crashing(tmp_path: Path) -> None:
    from awf.service.controls import WorkspaceStackStopError, _docker_process

    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    original_path = os.environ.get("PATH")
    os.environ["PATH"] = str(empty_bin)
    try:
        with pytest.raises(WorkspaceStackStopError) as exc_info:
            await _docker_process("ps", operation="ps")
    finally:
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path

    assert exc_info.value.returncode == 127
    assert "docker executable is not available" in exc_info.value.stderr


@pytest.mark.unit
async def test_docker_process_reports_os_error_without_crashing(tmp_path: Path) -> None:
    from awf.service.controls import WorkspaceStackStopError, _docker_process

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_path = bin_dir / "docker"
    docker_path.write_text("#!/bin/sh\necho should not run\n", encoding="utf-8")
    docker_path.chmod(0o644)

    original_path = os.environ.get("PATH")
    os.environ["PATH"] = str(bin_dir)
    try:
        with pytest.raises(WorkspaceStackStopError) as exc_info:
            await _docker_process("ps", operation="ps")
    finally:
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path

    assert exc_info.value.returncode == 1
    assert "PermissionError" in exc_info.value.stderr


@pytest.mark.unit
async def test_http_get_fetches_json_from_local_health_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    class _AsyncClient:
        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            calls.append((url, timeout))
            return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(status_mod.httpx, "AsyncClient", _AsyncClient)

    response = await _http_get("http://127.0.0.1:12345/healthz", timeout=5.0)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert calls == [("http://127.0.0.1:12345/healthz", 5.0)]


@pytest.mark.unit
def test_status_helper_extracts_supported_workspace_project_prefixes() -> None:
    assert _workspace_id_from_project("awf_ws_three") == "ws_three"
    assert _workspace_id_from_project("awf-ws_four") == "ws_four"
    assert _workspace_id_from_project("awf_not_workspace") is None
    assert _docker_socket_path("tcp://docker:2375") is None
    assert str(_docker_socket_path("unix:///var/run/docker.sock")) == "/var/run/docker.sock"
    assert len(_truncate("x" * 300)) == 240
    assert _truncate("ok") == "ok"


@pytest.mark.unit
def test_run_subprocess_wrapper_and_fail_without_detail() -> None:
    result = _run_subprocess(
        [sys.executable, "-c", "print('wrapped')"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5.0,
        env={},
    )

    assert result.returncode == 0
    assert result.stdout == "wrapped\n"
    assert _fail("NO_DETAIL", "") == {"ok": False, "status": "fail", "reason": "NO_DETAIL"}


@pytest.mark.unit
def test_orphan_check_treats_active_workspace_containers_as_expected(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        _container(
            id="abc",
            name="awf_ws_alive-agent-1",
            state="running",
            status="Up 3 minutes",
            project="awf_ws_alive",
            service="agent",
        ),
        _container(
            id="def",
            name="awf_ws_alive-postgres-1",
            state="running",
            status="Up 3 minutes (healthy)",
            project="awf_ws_alive",
            service="postgres",
        ),
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
            provider_environ={},
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
        _container(
            id="abc",
            name="awf_ws_dead-agent-1",
            state="running",
            status="Up 1 day",
            project="awf_ws_dead",
            service="agent",
        )
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
            provider_environ={},
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
    assert example["classification"] == "cleanup_ready"
    assert example["reason"] == "WORKSPACE_TERMINAL_LIVE_RUNTIME"
    action = orphans.get("action")
    assert isinstance(action, str) and action.strip()


@pytest.mark.unit
def test_orphan_check_flags_workspace_missing_from_db(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        _container(
            id="ghost",
            name="awf-ws_ghost-agent-1",
            state="exited",
            status="Exited (0) 5 minutes ago",
            project="awf-ws_ghost",
            service="agent",
        )
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
            provider_environ={},
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is False
    assert orphans["reason"] == "ORPHANS_PRESENT"
    examples = orphans["examples"]
    assert examples[0]["workspace_id"] == "ws_ghost"
    assert examples[0]["compose_project"] == "awf-ws_ghost"
    assert examples[0]["classification"] == "cleanup_ready"
    assert examples[0]["reason"] == "WORKSPACE_MISSING"


@pytest.mark.unit
def test_orphan_check_skips_non_awf_compose_projects(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        _container(
            id="x",
            name="myapp-db-1",
            state="running",
            status="Up",
            project="myapp",
            service="db",
        )
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
            provider_environ={},
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["ok"] is True
    assert orphans["reason"] == "NO_ORPHANS"
    assert orphans["orphan_count"] == 0
