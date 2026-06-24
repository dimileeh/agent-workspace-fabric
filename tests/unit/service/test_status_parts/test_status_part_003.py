"""Service status stranded-workspace checks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from awf.db.enums import WorkspaceStatus
from awf.service.status import (
    WorkspaceIdView,
    WorkspaceLifecycleSnapshot,
    collect_service_status,
)
from tests.unit.service.test_status_parts.test_status_part_001 import (
    _api_get,
    _container,
    _db_probe,
    _DiskUsage,
    _docker_ps_payload,
    _make_run_subprocess,
    _settings,
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
