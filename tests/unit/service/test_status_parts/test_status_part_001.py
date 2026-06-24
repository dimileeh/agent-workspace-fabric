"""Service status disk-pressure and orphan-container checks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
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
    _default_workspace_id_lookup,
    _is_legacy_open_default_workspace,
    _orphan_resources_check_payload,
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


async def _worker_reaper_ok(_settings: ServiceSettings) -> dict[str, Any]:
    return {"ok": True, "status": "ok", "reason": "WORKER_HEARTBEAT_FRESH"}


async def _worker_reaper_missing(_settings: ServiceSettings) -> dict[str, Any]:
    return {"ok": False, "status": "fail", "reason": "WORKER_HEARTBEAT_MISSING"}


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


class _WorkerReaperSessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _WorkerReaperEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def _patch_worker_reaper_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    latest_for_node: Any,
) -> _WorkerReaperEngine:
    engine = _WorkerReaperEngine()

    class _WorkerHeartbeatRepository:
        def __init__(self, _session: object) -> None:
            return None

        async def latest_for_node(self, *, node_id: str) -> object:
            return await latest_for_node(node_id=node_id)

    monkeypatch.setattr(status_mod, "make_engine", lambda _database_url: engine)
    monkeypatch.setattr(
        status_mod,
        "make_session_factory",
        lambda _engine: lambda: _WorkerReaperSessionContext(),
    )
    monkeypatch.setattr(status_mod, "WorkerHeartbeatRepository", _WorkerHeartbeatRepository)
    return engine


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
def test_orphan_resources_check_payload_blocks_reaping_when_scans_warn() -> None:
    payload = _orphan_resources_check_payload(
        {
            "ok": False,
            "status": "fail",
            "reason": "ORPHANS_PRESENT",
            "orphan_count": 1,
            "warning_count": 0,
            "warnings": [{"resource_kind": "network", "reason": "partial_scan"}],
        },
        auto_cleanup_orphans=True,
    )

    assert payload["ok"] is False
    assert payload["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert payload["cleanup_readiness"] == {
        "ready": False,
        "status": "blocked",
        "reason": "ORPHAN_RESOURCES_PRESENT",
        "action": "Review the listed AWF resources before running cleanup.",
        "dry_run_only": True,
    }


@pytest.mark.unit
def test_orphan_resources_check_payload_enables_reaping_for_boolean_orphan_count() -> None:
    payload = _orphan_resources_check_payload(
        {
            "ok": False,
            "status": "fail",
            "reason": "ORPHANS_PRESENT",
            "orphan_count": True,
        },
        auto_cleanup_orphans=True,
    )

    assert payload["ok"] is True
    assert payload["reason"] == "ORPHANS_PRESENT_REAPING_ENABLED"
    assert payload["cleanup_readiness"] == {
        "ready": True,
        "status": "ready",
        "reason": "ORPHANS_PRESENT_REAPING_ENABLED",
        "action": status_mod.ORPHAN_REAPING_ACTION,
        "dry_run_only": False,
    }


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
        "cursor",
        "gemini",
        "opencode",
        "grok",
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
def test_service_status_uses_caller_environ_with_compose_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text("AWF_GITHUB_TOKEN=file-token\n", encoding="utf-8")
    captured: dict[str, str] = {}

    def _collect_agent_readiness(
        _settings: ServiceSettings,
        *,
        environ: Mapping[str, str],
        **_kwargs: object,
    ) -> dict[str, object]:
        captured.update(environ)
        return {"status": "ok", "providers": {}}

    async def _egress_lookup(_database_url: str) -> Mapping[str, int]:
        return {}

    monkeypatch.setattr(status_mod, "collect_agent_readiness", _collect_agent_readiness)
    monkeypatch.delenv("AWF_GITHUB_TOKEN", raising=False)

    status = asyncio.run(
        collect_service_status(
            replace(_settings(tmp_path), workspace_cleanup_enabled=False),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            compose_env_file=compose_env_file,
            environ={"AWF_GITHUB_TOKEN": "caller-token"},
            egress_audit_summary_lookup=_egress_lookup,
        )
    )

    assert status["agent_readiness"]["status"] == "ok"
    assert captured["AWF_GITHUB_TOKEN"] == "caller-token"


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
