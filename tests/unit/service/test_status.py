"""Service status disk-pressure and orphan-container checks."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import InterfaceError

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import EgressAuditRepository, WorkspaceRepository
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
    _is_legacy_open_default_workspace,
    _orphan_resources_check_payload,
    _run_docker_command,
    _run_subprocess,
    _truncate,
    _workspace_id_from_project,
    check_database,
    collect_egress_audit_status,
    collect_service_status,
    collect_workspace_cleanup_status,
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
            provider_environ={},
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
async def test_service_status_exposes_workspace_cleanup_readiness(tmp_path: Path) -> None:
    async with postgres_test_url() as database_url:
        now = datetime.now(UTC)
        old_completed = await _seed_cleanup_status_workspace(
            database_url,
            status=WorkspaceStatus.completed,
            updated_at=now - timedelta(hours=200),
            title="old completed",
            pr=True,
        )
        recent_completed = await _seed_cleanup_status_workspace(
            database_url,
            status=WorkspaceStatus.completed,
            updated_at=now - timedelta(hours=2),
            title="recent completed",
            pr=True,
        )
        failed = await _seed_cleanup_status_workspace(
            database_url,
            status=WorkspaceStatus.failed,
            updated_at=now - timedelta(hours=200),
            title="failed",
        )
        settings = _settings(
            tmp_path,
            database_url=database_url,
            completed_workspace_retention_hours=24,
        )

        status = await collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            provider_environ={},
        )

    assert status["status"] == "ok"
    cleanup = status["checks"]["workspace_cleanup"]
    assert cleanup["ok"] is True
    assert cleanup["status"] == "ready"
    assert cleanup["reason"] == "CLEANUP_CANDIDATES_READY"
    assert cleanup["retention_hours"] == 24
    assert cleanup["candidate_count"] == 1
    assert cleanup["preserved_count"] == 2
    examples = cleanup["examples"]
    assert {example["workspace_id"] for example in examples} == {
        old_completed,
        recent_completed,
        failed,
    }
    assert {example["reason_code"] for example in examples} == {
        "COMPLETED_PR_RETENTION_EXPIRED",
        "WORKSPACE_WITHIN_RETENTION",
        "FAILED_WORKSPACE_TRIAGE_PRESERVED",
    }


@pytest.mark.unit
async def test_service_status_includes_egress_audit_posture_counts(tmp_path: Path) -> None:
    async with postgres_test_url() as database_url:
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/repo.git",
                branch_base="development",
                task_title="egress audited",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            workspace.status = WorkspaceStatus.running.value
            await EgressAuditRepository(session).create(
                workspace_id=workspace.id,
                attempt_id=None,
                policy_posture="restricted",
                decision="deferred",
                destination_category="allowlisted_public",
                reason_code="LOCAL_EGRESS_RESTRICTED_ALLOWLIST",
                details={},
            )
            await session.commit()
        await engine.dispose()

        status = await collect_service_status(
            _settings(tmp_path, database_url=database_url),
            api_get=_api_get,
            run_subprocess=_make_run_subprocess(),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            provider_environ={},
        )

    egress_audit = status["checks"]["egress_audit"]
    assert egress_audit["ok"] is True
    assert egress_audit["status"] == "ok"
    assert egress_audit["reason"] == "EGRESS_AUDIT_AVAILABLE"
    assert egress_audit["resource_count"] == 1
    assert egress_audit["egress_posture_counts"] == {"restricted": 1}


@pytest.mark.unit
async def test_egress_audit_status_redacts_unavailable_detail() -> None:
    async def _failing_lookup(_database_url: str) -> dict[str, int]:
        raise RuntimeError(
            "could not connect to "
            "postgresql+asyncpg://awf:db_password@db.internal:5432/awf; "
            "Authorization: Bearer ghp_secret1234567890; "
            f"{'x' * 400}"
        )

    egress_audit = await collect_egress_audit_status(
        "postgresql+asyncpg://awf:db_password@db.internal:5432/awf",
        summary_lookup=_failing_lookup,
    )

    detail = str(egress_audit["detail"])
    assert egress_audit["ok"] is True
    assert egress_audit["status"] == "unavailable"
    assert egress_audit["reason"] == "EGRESS_AUDIT_UNAVAILABLE"
    assert len(detail) <= 240
    assert "db_password" not in detail
    assert "ghp_secret1234567890" not in detail
    assert "postgresql+asyncpg://[redacted]@db.internal:5432/awf" in detail
    assert "Authorization: Bearer [redacted]" in detail


@pytest.mark.unit
async def test_egress_audit_status_reports_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _slow_lookup(_database_url: str) -> dict[str, int]:
        raise AssertionError("wait_for should timeout before the lookup resolves")

    def _raise_timeout(awaitable: object, *, timeout: float) -> None:
        assert timeout == status_mod._CHECK_TIMEOUT_SECONDS
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise TimeoutError

    monkeypatch.setattr(status_mod.asyncio, "wait_for", _raise_timeout)

    egress_audit = await collect_egress_audit_status(
        "postgresql+asyncpg://awf:db_password@db.internal:5432/awf",
        summary_lookup=_slow_lookup,
    )

    assert egress_audit["ok"] is True
    assert egress_audit["status"] == "unknown"
    assert egress_audit["reason"] == "EGRESS_AUDIT_TIMEOUT"
    assert egress_audit["resource_count"] == 0
    assert egress_audit["egress_posture_counts"] == {}


@pytest.mark.unit
def test_workspace_cleanup_status_reports_disabled_policy(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), workspace_cleanup_enabled=False)

    cleanup = asyncio.run(collect_workspace_cleanup_status(settings))

    assert cleanup == {
        "ok": True,
        "status": "disabled",
        "reason": "WORKSPACE_CLEANUP_DISABLED",
        "retention_hours": 168,
        "candidate_count": 0,
        "preserved_count": 0,
        "examples": [],
    }


@pytest.mark.unit
def test_workspace_cleanup_status_reports_plan_unavailable_without_engine_dispose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        status_mod,
        "make_engine",
        lambda _url: (_ for _ in ()).throw(RuntimeError("database config missing")),
    )

    cleanup = asyncio.run(collect_workspace_cleanup_status(_settings(tmp_path)))

    assert cleanup["ok"] is True
    assert cleanup["status"] == "unavailable"
    assert cleanup["reason"] == "CLEANUP_PLAN_UNAVAILABLE"
    assert "database config missing" in cleanup["detail"]


@pytest.mark.unit
def test_workspace_cleanup_status_disposes_engine_when_plan_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    disposed = False

    class _Engine:
        async def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    async def fail_plan(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise RuntimeError("planner failed")

    monkeypatch.setattr(status_mod, "make_engine", lambda _url: _Engine())
    monkeypatch.setattr(status_mod, "make_session_factory", lambda _engine: object())
    monkeypatch.setattr(status_mod, "plan_terminal_workspace_gc", fail_plan)

    cleanup = asyncio.run(collect_workspace_cleanup_status(_settings(tmp_path)))

    assert cleanup["ok"] is True
    assert cleanup["status"] == "unavailable"
    assert cleanup["reason"] == "CLEANUP_PLAN_UNAVAILABLE"
    assert "planner failed" in cleanup["detail"]
    assert disposed is True


@pytest.mark.unit
def test_status_db_helpers_handle_engine_construction_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status_mod,
        "make_engine",
        lambda _url: (_ for _ in ()).throw(RuntimeError("bad database url")),
    )

    db = asyncio.run(check_database(_POSTGRES_TEST_URL))
    view = asyncio.run(_default_workspace_id_lookup(_POSTGRES_TEST_URL))

    assert db["ok"] is False
    assert db["reason"] == "DB_CONNECTION_FAILED"
    assert view.available is False


@pytest.mark.unit
def test_check_database_disposes_engine_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed = False

    class _Connection:
        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("select failed")

    class _Engine:
        def connect(self) -> _Connection:
            return _Connection()

        async def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    monkeypatch.setattr(status_mod, "make_engine", lambda _url: _Engine())

    db = asyncio.run(check_database(_POSTGRES_TEST_URL))

    assert db["ok"] is False
    assert db["reason"] == "DB_CONNECTION_FAILED"
    assert "select failed" in str(db["detail"])
    assert disposed is True


@pytest.mark.unit
def test_legacy_open_default_treats_non_datetime_rows_as_legacy() -> None:
    cutoff = datetime(2026, 5, 4, tzinfo=UTC)

    assert (
        _is_legacy_open_default_workspace(
            "legacy-string-created-at",
            legacy_open_default_cutoff=cutoff,
        )
        is True
    )


@pytest.mark.unit
async def test_default_workspace_lookup_extracts_network_posture_from_resolved_profile(
    tmp_path: Path,
) -> None:
    async with postgres_test_url() as database_url:
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        try:
            async with factory() as session:
                workspace = await WorkspaceRepository(session).create(
                    repo_url="git@github.com:example/repo.git",
                    branch_base="development",
                    task_title="open profile",
                    task_prompt="p",
                    agent="codex",
                    test_commands=[],
                    resolved_profile={
                        "name": "open-profile",
                        "security": {"egress": {"mode": "open"}},
                    },
                )
                workspace.status = WorkspaceStatus.running.value
                workspace.created_at = datetime(2026, 5, 2, 11, 20, 37, tzinfo=UTC)
                await session.commit()
                workspace_id = workspace.id
            view = await _default_workspace_id_lookup(database_url)
        finally:
            await engine.dispose()

    assert view.available is True
    assert view.active_ids == frozenset({workspace_id})
    assert view.snapshots[0].network_posture == "open"


@pytest.mark.unit
async def test_default_workspace_lookup_keeps_open_when_legacy_cutoff_unset(
    tmp_path: Path,
) -> None:
    async with postgres_test_url() as database_url:
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        try:
            async with factory() as session:
                workspace = await WorkspaceRepository(session).create(
                    repo_url="git@github.com:example/repo.git",
                    branch_base="development",
                    task_title="explicit open profile",
                    task_prompt="p",
                    agent="codex",
                    test_commands=[],
                    resolved_profile={
                        "name": "explicit-open-profile",
                        "security": {"egress": {"mode": "open"}},
                    },
                )
                workspace.status = WorkspaceStatus.running.value
                workspace.created_at = datetime(2026, 5, 2, 11, 20, 35, tzinfo=UTC)
                await session.commit()
                workspace_id = workspace.id
            view = await _default_workspace_id_lookup(database_url)
        finally:
            await engine.dispose()

    assert view.available is True
    assert view.active_ids == frozenset({workspace_id})
    assert view.snapshots[0].network_posture == "open"


@pytest.mark.unit
async def test_default_workspace_lookup_treats_legacy_open_default_as_unknown(
    tmp_path: Path,
) -> None:
    async with postgres_test_url() as database_url:
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        try:
            async with factory() as session:
                workspace = await WorkspaceRepository(session).create(
                    repo_url="git@github.com:example/repo.git",
                    branch_base="development",
                    task_title="legacy open default",
                    task_prompt="p",
                    agent="codex",
                    test_commands=[],
                    resolved_profile={
                        "name": "legacy-open-default",
                        "security": {"egress": {"mode": "open"}},
                    },
                )
                workspace.status = WorkspaceStatus.running.value
                workspace.created_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
                await session.commit()
                workspace_id = workspace.id
            view = await _default_workspace_id_lookup(
                database_url,
                legacy_open_default_cutoff=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
            )
        finally:
            await engine.dispose()

    assert view.available is True
    assert view.active_ids == frozenset({workspace_id})
    assert view.snapshots[0].network_posture is None


@pytest.mark.unit
def test_orphan_resources_check_payload_handles_missing_resource_counts() -> None:
    payload = _orphan_resources_check_payload(
        {
            "ok": True,
            "status": "ok",
            "reason": "NO_ORPHANS",
            "cleanup_readiness": {"ready": True},
        }
    )

    assert payload["reason"] == "NO_ORPHANS"
    assert payload["cleanup_readiness"]["ready"] is True
    assert payload["cleanup_readiness"]["reason"] == "NO_ORPHANS"
    assert "counts_by_kind" not in payload


@pytest.mark.unit
def test_service_status_provider_warnings_do_not_fail_by_default(tmp_path: Path) -> None:
    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            provider_environ={},
        )
    )

    assert status["status"] == "ok"
    readiness = status["agent_readiness"]
    assert readiness["status"] == "ok"
    assert set(readiness["providers"]) == {
        "github",
        "codex",
        "claude_code",
        "gemini",
        "opencode",
        "docker",
    }
    assert readiness["providers"]["github"]["status"] == "warn"
    assert readiness["providers"]["github"]["reason"] == "GITHUB_TOKEN_ENV_MISSING"
    assert readiness["providers"]["docker"]["status"] == "ok"
    assert "DOCKER_HOST_BROAD_CONTROL" in readiness["security"]["reason_codes"]


@pytest.mark.unit
def test_service_status_includes_network_posture_counts_and_open_warning(
    tmp_path: Path,
) -> None:
    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset(
                {
                    "ws_open",
                    "ws_restricted",
                    "ws_offline",
                    "ws_unknown",
                    "ws_missing_snapshot",
                }
            ),
            terminal_ids=frozenset(),
            available=True,
            snapshots=(
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_open",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    network_posture="open",
                ),
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_restricted",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    network_posture="restricted",
                ),
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_offline",
                    status=WorkspaceStatus.ready.value,
                    updated_at=datetime.now(UTC),
                    network_posture="offline",
                ),
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_unknown",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    network_posture=None,
                ),
            ),
        )

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=""),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
            provider_environ={},
        )
    )

    posture = status["checks"]["network_posture"]
    assert posture["ok"] is True
    assert posture["status"] == "warn"
    assert posture["reason"] == "NETWORK_POSTURE_OPEN_ACTIVE"
    assert posture["active_counts_by_posture"] == {
        "restricted": 1,
        "offline": 1,
        "open": 1,
        "unknown": 2,
    }
    assert posture["open_examples"] == [
        {"workspace_id": "ws_open", "status": "running", "pr_url": None}
    ]
    assert "active_restricted_templates" in posture
    assert "deferred_enforcement_note" in posture


@pytest.mark.unit
def test_network_posture_payload_includes_restricted_templates(tmp_path: Path) -> None:
    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset({"ws_tpl"}),
            terminal_ids=frozenset(),
            available=True,
            snapshots=(
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_tpl",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    network_posture="restricted",
                ),
            ),
        )

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=""),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
            provider_environ={},
        )
    )

    posture = status["checks"]["network_posture"]
    assert posture["reason"] == "NETWORK_POSTURE_NO_ACTIVE_OPEN"
    assert posture["active_restricted_templates"] == []
    assert "Destination-level filtering" in posture["deferred_enforcement_note"]


@pytest.mark.unit
def test_network_posture_payload_aggregates_restricted_templates(tmp_path: Path) -> None:
    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset({"ws_1", "ws_2"}),
            terminal_ids=frozenset(),
            available=True,
            snapshots=(
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_1",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    network_posture="restricted",
                ),
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_2",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    network_posture="restricted",
                ),
            ),
        )

    def _workspace_lookup(url: str) -> WorkspaceIdView:
        return _ws_lookup_sync(url)

    def _ws_lookup_sync(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset({"ws_1", "ws_2"}),
            terminal_ids=frozenset(),
            available=True,
            snapshots=(
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_1",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    network_posture="restricted",
                    allowlist_templates=("tpl_a", "tpl_b"),
                ),
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_2",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    network_posture="restricted",
                    allowlist_templates=("tpl_b", "tpl_c"),
                ),
            ),
        )

    from awf.service.status import _network_posture_check_payload

    payload = _network_posture_check_payload(_ws_lookup_sync(""))
    assert payload["active_restricted_templates"] == ["tpl_a", "tpl_b", "tpl_c"]


@pytest.mark.unit
def test_service_status_reports_network_posture_unavailable_when_db_lookup_fails(
    tmp_path: Path,
) -> None:
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
            run_subprocess=_make_run_subprocess(ps_payload=""),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
            provider_environ={},
        )
    )

    posture = status["checks"]["network_posture"]
    assert posture["ok"] is True
    assert posture["status"] == "unknown"
    assert posture["reason"] == "NETWORK_POSTURE_UNAVAILABLE"


@pytest.mark.unit
def test_service_status_strict_provider_failure_sets_top_level_fail(tmp_path: Path) -> None:
    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            provider_environ={},
            strict_providers={"github"},
        )
    )

    assert status["status"] == "fail"
    readiness = status["agent_readiness"]
    assert readiness["status"] == "fail"
    assert readiness["providers"]["github"]["status"] == "fail"
    assert readiness["providers"]["github"]["reason"] == "GITHUB_TOKEN_ENV_MISSING"


@pytest.mark.unit
def test_service_status_strict_codex_provider_failure_sets_top_level_fail(
    tmp_path: Path,
) -> None:
    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            provider_environ={},
            strict_providers={"codex"},
        )
    )

    assert status["status"] == "fail"
    readiness = status["agent_readiness"]
    assert readiness["status"] == "fail"
    assert readiness["strict_providers"] == ["codex"]
    assert readiness["providers"]["codex"]["status"] == "fail"
    assert readiness["providers"]["codex"]["reason"] == "CODEX_AUTH_MISSING"
    assert "codex" in readiness["security"]["providers_with_warnings"]


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
            provider_environ={},
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
            provider_environ={},
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
def test_service_status_includes_orphan_resource_summary(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    worktree = Path(settings.work_dir) / "git" / "worktrees" / "ws_ghost"
    worktree.mkdir(parents=True)

    payload = _docker_ps_payload(
        _container(
            id="abc",
            name="awf_ws_ghost-agent-1",
            state="exited",
            status="Exited",
            project="awf_ws_ghost",
            service="agent",
        )
    )
    network_payload = _docker_ps_payload(
        {"id": "net", "name": "awf_ws_ghost_default", "project": "awf_ws_ghost"}
    )
    volume_payload = _docker_ps_payload(
        {"name": "awf_ws_ghost_postgres_data", "project": "awf_ws_ghost"}
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(
                ps_payload=payload,
                network_payload=network_payload,
                volume_payload=volume_payload,
            ),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            provider_environ={},
        )
    )

    assert status["status"] == "fail"
    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["reason"] == "ORPHANS_PRESENT"
    assert orphans["resource_counts"] == {
        "container": 1,
        "network": 1,
        "volume": 1,
        "worktree": 1,
    }
    assert orphans["orphan_counts_by_kind"] == {
        "container": 1,
        "network": 1,
        "volume": 1,
        "worktree": 1,
    }
    examples = orphans["examples"]
    assert {example["resource_kind"] for example in examples} == {
        "container",
        "network",
        "volume",
        "worktree",
    }
    assert all(example["workspace_id"] == "ws_ghost" for example in examples)
    assert isinstance(orphans["action"], str) and orphans["action"].strip()

    orphan_resources = status["checks"]["orphan_resources"]
    assert orphan_resources["ok"] is False
    assert orphan_resources["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert orphan_resources["orphan_count"] == 4
    assert orphan_resources["orphan_counts_by_kind"] == {
        "container": 1,
        "network": 1,
        "volume": 1,
        "worktree": 1,
    }
    assert orphan_resources["cleanup_readiness"]["dry_run_only"] is True


@pytest.mark.unit
def test_service_status_reports_stranded_active_workspaces(tmp_path: Path) -> None:
    payload = _docker_ps_payload(
        _container(
            id="pg",
            name="awf_ws_no_agent-postgres-1",
            state="running",
            status="Up 3 minutes",
            project="awf_ws_no_agent",
            service="postgres",
        ),
        _container(
            id="agent",
            name="awf_ws_exited-agent-1",
            state="exited",
            status="Exited (1) 2 minutes ago",
            project="awf_ws_exited",
            service="agent",
        ),
    )

    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset(
                {
                    "ws_missing_stack",
                    "ws_no_agent",
                    "ws_exited",
                    "ws_monitor",
                }
            ),
            terminal_ids=frozenset(),
            available=True,
            snapshots=(
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_missing_stack",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    compose_project_name="awf_ws_missing_stack",
                    compose_file_path="/tmp/ws_missing_stack/compose.yml",
                ),
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_no_agent",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    compose_project_name="awf_ws_no_agent",
                    compose_file_path="/tmp/ws_no_agent/compose.yml",
                ),
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_exited",
                    status=WorkspaceStatus.running.value,
                    updated_at=datetime.now(UTC),
                    compose_project_name="awf_ws_exited",
                    compose_file_path="/tmp/ws_exited/compose.yml",
                ),
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_monitor",
                    status=WorkspaceStatus.monitoring_pr.value,
                    updated_at=datetime.now(UTC),
                    compose_project_name="awf_ws_monitor",
                    compose_file_path="/tmp/ws_monitor/compose.yml",
                    pr_url="https://github.com/example/repo/pull/42",
                ),
            ),
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
    stranded = status["checks"]["stranded_workspaces"]
    assert stranded["ok"] is False
    assert stranded["status"] == "fail"
    assert stranded["reason"] == "STRANDED_WORKSPACES_PRESENT"
    assert stranded["stranded_count"] == 4
    assert stranded["fail_candidate_count"] == 3
    assert stranded["recoverable_count"] == 1
    assert stranded["reason_counts"] == {
        "AGENT_CONTAINER_EXITED": 1,
        "AGENT_CONTAINER_MISSING": 1,
        "STRANDED_WORKSPACE": 2,
    }
    examples = stranded["examples"]
    assert {example["workspace_id"] for example in examples} == {
        "ws_missing_stack",
        "ws_no_agent",
        "ws_exited",
        "ws_monitor",
    }
    assert any(
        example["workspace_id"] == "ws_monitor" and example["decision"] == "remonitor_workspace"
        for example in examples
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert orphans["reason"] == "NO_ORPHANS"


@pytest.mark.unit
def test_service_status_recoverable_monitoring_pr_stranding_does_not_fail_service(
    tmp_path: Path,
) -> None:
    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset({"ws_monitor"}),
            terminal_ids=frozenset(),
            available=True,
            snapshots=(
                WorkspaceLifecycleSnapshot(
                    workspace_id="ws_monitor",
                    status=WorkspaceStatus.monitoring_pr.value,
                    updated_at=datetime.now(UTC),
                    compose_project_name="awf_ws_monitor",
                    compose_file_path="/tmp/ws_monitor/compose.yml",
                    pr_url="https://github.com/example/repo/pull/42",
                ),
            ),
        )

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=""),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
            provider_environ={},
        )
    )

    assert status["status"] == "ok"
    stranded = status["checks"]["stranded_workspaces"]
    assert stranded["ok"] is True
    assert stranded["status"] == "recoverable"
    assert stranded["reason"] == "STRANDED_WORKSPACES_RECOVERABLE"
    assert stranded["fail_candidate_count"] == 0
    assert stranded["recoverable_count"] == 1


@pytest.mark.unit
def test_service_status_does_not_fail_active_id_without_lifecycle_snapshot(
    tmp_path: Path,
) -> None:
    async def _ws_lookup(_url: str) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset({"ws_monitor"}),
            terminal_ids=frozenset(),
            available=True,
            snapshots=(),
        )

    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=""),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
            provider_environ={},
        )
    )

    assert status["status"] == "ok"
    stranded = status["checks"]["stranded_workspaces"]
    assert stranded["ok"] is True
    assert stranded["status"] == "ok"
    assert stranded["fail_candidate_count"] == 0
    assert stranded["examples"] == []


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
    assert example["reason"] == "WORKSPACE_TERMINAL_RETENTION_EXPIRED"
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
def test_service_status_flags_completed_container_within_retention_as_live_leak(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    (Path(settings.work_dir) / "git" / "worktrees" / "ws_done").mkdir(parents=True)
    payload = _docker_ps_payload(
        _container(
            id="abc",
            name="awf_ws_done-agent-1",
            state="exited",
            status="Exited",
            project="awf_ws_done",
            service="agent",
        )
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
            run_subprocess=_make_run_subprocess(ps_payload=payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_ws_lookup,
            provider_environ={},
        )
    )

    orphans = status["checks"]["orphan_workspaces"]
    assert status["status"] == "fail"
    assert orphans["reason"] == "ORPHANS_PRESENT"
    assert orphans["orphan_count"] == 1
    assert orphans["leaked_live_count"] == 1
    assert orphans["leaked_live_counts_by_kind"] == {"container": 1}
    assert orphans["retained_evidence_count"] == 1
    assert orphans["retained_evidence_counts_by_kind"] == {"worktree": 1}


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
async def test_http_get_fetches_json_from_local_health_server() -> None:
    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 15\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b'{"status":"ok"}'
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    try:
        socket = server.sockets[0]
        host, port = socket.getsockname()[:2]
        response = await _http_get(f"http://{host}:{port}/healthz", timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
