"""Service status stranded-workspace checks."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.db.enums import WorkspaceStatus
from awf.service import status as status_mod
from awf.service.status import (
    WorkspaceIdView,
    WorkspaceLifecycleSnapshot,
    _check_worker_reaper,
    _orphan_resources_check_payload,
    collect_service_status,
)
from tests.unit.service.test_status_parts.test_status_part_001 import (
    _api_get,
    _container,
    _db_probe,
    _DiskUsage,
    _docker_ps_payload,
    _empty_workspace_view,
    _make_run_subprocess,
    _patch_worker_reaper_lookup,
    _settings,
    _worker_reaper_missing,
    _worker_reaper_ok,
)


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
async def test_worker_reaper_check_reports_fresh_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: dict[str, str] = {}

    async def latest_for_node(*, node_id: str) -> object:
        seen["node_id"] = node_id
        return SimpleNamespace(
            last_heartbeat_at=datetime.now(UTC),
            poll_interval_seconds=0.1,
        )

    engine = _patch_worker_reaper_lookup(monkeypatch, latest_for_node=latest_for_node)

    result = await _check_worker_reaper(_settings(tmp_path))

    assert result == {
        "ok": True,
        "status": "ok",
        "reason": "WORKER_HEARTBEAT_FRESH",
        "detail": "Latest worker heartbeat is fresh for node 'local'",
        "resource_count": 1,
    }
    assert seen == {"node_id": "local"}
    assert engine.disposed is True


@pytest.mark.unit
async def test_worker_reaper_check_reports_missing_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def latest_for_node(*, node_id: str) -> object:
        assert node_id == "local"
        return None

    engine = _patch_worker_reaper_lookup(monkeypatch, latest_for_node=latest_for_node)

    result = await _check_worker_reaper(_settings(tmp_path))

    assert result == {
        "ok": False,
        "status": "fail",
        "reason": "WORKER_HEARTBEAT_MISSING",
        "detail": "No worker heartbeat recorded for node 'local'",
    }
    assert engine.disposed is True


@pytest.mark.unit
async def test_worker_reaper_check_reports_stale_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def latest_for_node(*, node_id: str) -> object:
        assert node_id == "local"
        return SimpleNamespace(
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=30),
            poll_interval_seconds=0.1,
        )

    engine = _patch_worker_reaper_lookup(monkeypatch, latest_for_node=latest_for_node)

    result = await _check_worker_reaper(_settings(tmp_path))

    assert result["ok"] is False
    assert result["status"] == "fail"
    assert result["reason"] == "WORKER_HEARTBEAT_STALE"
    assert "stale after 15.0s" in str(result["detail"])
    assert engine.disposed is True


@pytest.mark.unit
async def test_worker_reaper_check_reports_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def latest_for_node(*, node_id: str) -> object:
        assert node_id == "local"
        await asyncio.sleep(0.01)
        return None

    monkeypatch.setattr(status_mod, "_CHECK_TIMEOUT_SECONDS", 0.001)
    engine = _patch_worker_reaper_lookup(monkeypatch, latest_for_node=latest_for_node)

    result = await _check_worker_reaper(_settings(tmp_path))

    assert result == {
        "ok": False,
        "status": "fail",
        "reason": "WORKER_HEARTBEAT_UNAVAILABLE",
        "detail": "Worker heartbeat lookup exceeded 0.001s",
    }
    assert engine.disposed is True


@pytest.mark.unit
async def test_worker_reaper_check_reports_repository_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def latest_for_node(*, node_id: str) -> object:
        assert node_id == "local"
        raise RuntimeError("heartbeat query failed")

    engine = _patch_worker_reaper_lookup(monkeypatch, latest_for_node=latest_for_node)

    result = await _check_worker_reaper(_settings(tmp_path))

    assert result["ok"] is False
    assert result["status"] == "fail"
    assert result["reason"] == "WORKER_HEARTBEAT_UNAVAILABLE"
    assert "heartbeat query failed" in str(result["detail"])
    assert engine.disposed is True


@pytest.mark.unit
def test_service_status_orphan_resources_reflect_auto_cleanup(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), auto_cleanup_orphans=True)
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

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            provider_environ={},
            worker_reaper_check=_worker_reaper_ok,
        )
    )

    orphan_resources = status["checks"]["orphan_resources"]
    assert orphan_resources["ok"] is True
    assert orphan_resources["status"] == "ok"
    assert orphan_resources["reason"] == "ORPHANS_PRESENT_REAPING_ENABLED"
    assert orphan_resources["action"] == status_mod.ORPHAN_REAPING_ACTION
    assert orphan_resources["cleanup_readiness"]["dry_run_only"] is False


@pytest.mark.unit
def test_service_status_orphan_resources_requires_live_reaper_for_auto_cleanup(
    tmp_path: Path,
) -> None:
    settings = replace(_settings(tmp_path), auto_cleanup_orphans=True)
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

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            provider_environ={},
            worker_reaper_check=_worker_reaper_missing,
        )
    )

    orphan_workspaces = status["checks"]["orphan_workspaces"]
    orphan_resources = status["checks"]["orphan_resources"]
    assert status["status"] == "fail"
    assert orphan_workspaces["ok"] is False
    assert orphan_workspaces["reason"] == "ORPHANS_PRESENT"
    assert orphan_resources["ok"] is False
    assert orphan_resources["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert orphan_resources["cleanup_readiness"]["dry_run_only"] is True
    assert orphan_resources["reaper_readiness"]["reason"] == "WORKER_HEARTBEAT_MISSING"


@pytest.mark.unit
def test_service_status_orphan_workspaces_action_aligns_with_reaping(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), auto_cleanup_orphans=True)
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

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            provider_environ={},
            worker_reaper_check=_worker_reaper_ok,
        )
    )

    orphan_workspaces = status["checks"]["orphan_workspaces"]
    orphan_resources = status["checks"]["orphan_resources"]
    assert orphan_workspaces["ok"] is True
    assert orphan_workspaces["status"] == "ok"
    assert orphan_workspaces["reason"] == "ORPHANS_PRESENT_REAPING_ENABLED"
    assert orphan_workspaces["action"] == status_mod.ORPHAN_REAPING_ACTION
    assert orphan_resources["action"] == status_mod.ORPHAN_REAPING_ACTION
    assert "Inspect the listed resources" not in orphan_workspaces["action"]


@pytest.mark.unit
def test_service_status_orphan_resources_uses_raw_payload_for_reaping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(_settings(tmp_path), auto_cleanup_orphans=True)
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
    original = status_mod._orphan_resources_check_payload
    observed: dict[str, object] = {}

    def _recording_orphan_resources_check(
        orphan_workspaces_check: Mapping[str, object],
        *,
        auto_cleanup_orphans: bool = False,
        reaper_available: bool = True,
        reaper_readiness: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        observed["status"] = orphan_workspaces_check.get("status")
        observed["reason"] = orphan_workspaces_check.get("reason")
        return original(
            orphan_workspaces_check,
            auto_cleanup_orphans=auto_cleanup_orphans,
            reaper_available=reaper_available,
            reaper_readiness=reaper_readiness,
        )

    monkeypatch.setattr(
        status_mod,
        "_orphan_resources_check_payload",
        _recording_orphan_resources_check,
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_make_run_subprocess(ps_payload=payload),
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            provider_environ={},
            worker_reaper_check=_worker_reaper_ok,
        )
    )

    orphan_workspaces = status["checks"]["orphan_workspaces"]
    orphan_resources = status["checks"]["orphan_resources"]
    assert observed == {"status": "fail", "reason": "ORPHANS_PRESENT"}
    assert orphan_workspaces["reason"] == "ORPHANS_PRESENT_REAPING_ENABLED"
    assert orphan_resources["reason"] == "ORPHANS_PRESENT_REAPING_ENABLED"


@pytest.mark.unit
def test_orphan_resources_check_payload_threads_auto_cleanup_flag() -> None:
    payload = _orphan_resources_check_payload(
        {
            "ok": False,
            "status": "fail",
            "reason": "ORPHANS_PRESENT",
            "orphan_count": 1,
        },
        auto_cleanup_orphans=True,
    )

    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["reason"] == "ORPHANS_PRESENT_REAPING_ENABLED"
    assert payload["cleanup_readiness"]["dry_run_only"] is False


@pytest.mark.unit
def test_orphan_resources_check_payload_aligns_action_with_reaping() -> None:
    legacy_action = (
        "Inspect the listed resources, then use the existing explicit workspace "
        "cleanup or service GC path after confirming no active workspace owns them."
    )
    payload = _orphan_resources_check_payload(
        {
            "ok": False,
            "status": "fail",
            "reason": "ORPHANS_PRESENT",
            "orphan_count": 1,
            "action": legacy_action,
        },
        auto_cleanup_orphans=True,
    )

    readiness = payload["cleanup_readiness"]
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["reason"] == "ORPHANS_PRESENT_REAPING_ENABLED"
    assert readiness["dry_run_only"] is False
    assert readiness["action"] != legacy_action
    assert "auto_cleanup_orphans" in readiness["action"]
    assert payload["action"] == readiness["action"]


@pytest.mark.unit
def test_orphan_resources_check_payload_keeps_legacy_action_without_reaping() -> None:
    legacy_action = (
        "Inspect the listed resources, then use the existing explicit workspace "
        "cleanup or service GC path after confirming no active workspace owns them."
    )
    payload = _orphan_resources_check_payload(
        {
            "ok": False,
            "status": "fail",
            "reason": "ORPHANS_PRESENT",
            "orphan_count": 1,
            "action": legacy_action,
        },
        auto_cleanup_orphans=False,
    )

    readiness = payload["cleanup_readiness"]
    assert readiness["dry_run_only"] is True
    assert readiness["action"] == legacy_action
    assert payload["action"] == legacy_action


@pytest.mark.unit
def test_orphan_resources_check_payload_does_not_reap_with_scanner_warning() -> None:
    legacy_action = (
        "Inspect the listed resources, then use the existing explicit workspace "
        "cleanup or service GC path after confirming no active workspace owns them."
    )
    payload = _orphan_resources_check_payload(
        {
            "ok": False,
            "status": "fail",
            "reason": "ORPHANS_PRESENT",
            "orphan_count": 1,
            "warning_count": 1,
            "warnings": [{"reason": "DOCKER_UNAVAILABLE"}],
            "action": legacy_action,
        },
        auto_cleanup_orphans=True,
    )

    readiness = payload["cleanup_readiness"]
    assert payload["ok"] is False
    assert payload["status"] == "fail"
    assert payload["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert readiness["dry_run_only"] is True
    assert readiness["action"] == legacy_action
