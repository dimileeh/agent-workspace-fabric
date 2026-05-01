from unittest.mock import patch
import asyncio
import pytest
from pathlib import Path
from datetime import datetime, UTC, timedelta
from awf.service.gc import (
    _classify_workspace_for_gc,
    _failed_terminal_workspace_has_no_work,
    _snapshot_has_no_work,
    _container_command_is_idle,
    WorkspaceGCCandidate,
    WorkspaceGCPreserved,
    COMPLETED_PR_RETENTION_EXPIRED,
    TERMINAL_WORKSPACE_RETENTION_EXPIRED,
    FAILED_WORKSPACE_NO_WORK,
    WORKSPACE_WITHIN_RETENTION
)
from awf.db.models import Workspace
from awf.db.enums import WorkspaceStatus
from awf.runtime.inspection import RuntimeSnapshot, RuntimeService

def test_snapshot_has_no_work_unavailable():
    snap = RuntimeSnapshot(stack_state="unavailable", services=[])
    assert _snapshot_has_no_work(snap) is False

def test_snapshot_has_no_work_no_agent():
    snap = RuntimeSnapshot(stack_state="running", services=[RuntimeService(name="other", state="running", command="sleep", container_id="id", image="img")])
    assert _snapshot_has_no_work(snap) is False

def test_container_command_is_idle_no_command():
    assert _container_command_is_idle(None) is False
    assert _container_command_is_idle("") is False

def test_classify_workspace_completed_retention_expired():
    # Workspace > 24 hours old, completed, has PR
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.completed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
        pr_url="http://github.com/pr/1"
    )
    res = _classify_workspace_for_gc(ws, work_dir=Path("/tmp"), now=datetime.now(UTC), cutoff_at=datetime.now(UTC) - timedelta(hours=24), default_policy=False, cleanup_enabled=True)
    assert isinstance(res, WorkspaceGCCandidate)
    assert res.reason_code == COMPLETED_PR_RETENTION_EXPIRED
    
    ws2 = Workspace(
        id="ws_2",
        status=WorkspaceStatus.completed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
        pr_url=None
    )
    res2 = _classify_workspace_for_gc(ws2, work_dir=Path("/tmp"), now=datetime.now(UTC), cutoff_at=datetime.now(UTC) - timedelta(hours=24), default_policy=False, cleanup_enabled=True)
    assert isinstance(res2, WorkspaceGCCandidate)
    assert res2.reason_code == TERMINAL_WORKSPACE_RETENTION_EXPIRED

def test_failed_terminal_workspace_has_no_work_exception():
    ws = Workspace(id="ws_1", compose_project_name="proj")
    with patch("awf.service.gc.asyncio.run", side_effect=Exception("mocked err")):
        assert _failed_terminal_workspace_has_no_work(ws) is False

def test_classify_workspace_failed_no_work_but_within_retention():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=1),
        compose_project_name="proj"
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=True):
        res = _classify_workspace_for_gc(ws, work_dir=Path("/tmp"), now=datetime.now(UTC), cutoff_at=datetime.now(UTC) - timedelta(hours=24), default_policy=True, cleanup_enabled=True)
        assert isinstance(res, WorkspaceGCPreserved)
        assert res.reason_code == WORKSPACE_WITHIN_RETENTION

def test_classify_workspace_failed_no_work_expired():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj"
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=True):
        res = _classify_workspace_for_gc(ws, work_dir=Path("/tmp"), now=datetime.now(UTC), cutoff_at=datetime.now(UTC) - timedelta(hours=24), default_policy=True, cleanup_enabled=True)
        assert isinstance(res, WorkspaceGCCandidate)
        assert res.reason_code == FAILED_WORKSPACE_NO_WORK

def test_classify_workspace_failed_has_work_but_expired():
    # Covers 1058
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj"
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        res = _classify_workspace_for_gc(ws, work_dir=Path("/tmp"), now=datetime.now(UTC), cutoff_at=datetime.now(UTC) - timedelta(hours=24), default_policy=True, cleanup_enabled=True)
        assert isinstance(res, WorkspaceGCPreserved)

