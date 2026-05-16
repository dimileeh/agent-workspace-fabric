from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.gc import (
    COMPLETED_PR_RETENTION_EXPIRED,
    FAILED_WORKSPACE_NO_WORK,
    TERMINAL_WORKSPACE_RETENTION_EXPIRED,
    WORKSPACE_WITHIN_RETENTION,
    WorkspaceGCCandidate,
    WorkspaceGCPreserved,
    _classify_workspace_for_gc,
    _container_command_is_idle,
    _failed_terminal_workspace_has_no_work,
    _snapshot_has_no_work,
)


def test_snapshot_has_no_work_unavailable():
    snap = RuntimeSnapshot(stack_state="unavailable", services=[])
    assert _snapshot_has_no_work(snap) is False


def test_snapshot_has_no_work_no_agent():
    snap = RuntimeSnapshot(
        stack_state="running",
        services=[
            RuntimeService(
                name="other", state="running", command="sleep", container_id="id", image="img"
            )
        ],
    )
    assert _snapshot_has_no_work(snap) is False


def test_container_command_is_idle_no_command():
    assert _container_command_is_idle(None) is False
    assert _container_command_is_idle("") is False


def test_classify_workspace_completed_retention_expired():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.completed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
        pr_url="http://github.com/pr/1",
    )
    res = _classify_workspace_for_gc(
        ws,
        work_dir=Path("/tmp"),
        now=datetime.now(UTC),
        cutoff_at=datetime.now(UTC) - timedelta(hours=24),
        default_policy=False,
        cleanup_enabled=True,
    )
    assert isinstance(res, WorkspaceGCCandidate)
    assert res.reason_code == COMPLETED_PR_RETENTION_EXPIRED

    ws2 = Workspace(
        id="ws_2",
        status=WorkspaceStatus.completed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
        pr_url=None,
    )
    res2 = _classify_workspace_for_gc(
        ws2,
        work_dir=Path("/tmp"),
        now=datetime.now(UTC),
        cutoff_at=datetime.now(UTC) - timedelta(hours=24),
        default_policy=False,
        cleanup_enabled=True,
    )
    assert isinstance(res2, WorkspaceGCCandidate)
    assert res2.reason_code == TERMINAL_WORKSPACE_RETENTION_EXPIRED


def test_failed_terminal_workspace_has_no_work_exception(monkeypatch):
    class _RaisingRuntimeInspector:
        async def inspect(self, compose_project_name: str) -> RuntimeSnapshot:
            assert compose_project_name == "proj"
            raise RuntimeError("mocked err")

    ws = Workspace(id="ws_1", compose_project_name="proj")
    monkeypatch.setattr("awf.service.gc._RUNTIME_INSPECTOR", _RaisingRuntimeInspector())

    assert _failed_terminal_workspace_has_no_work(ws) is False


def test_classify_workspace_failed_no_work_but_within_retention():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=1),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=True):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/tmp"),
            now=datetime.now(UTC),
            cutoff_at=datetime.now(UTC) - timedelta(hours=24),
            default_policy=True,
            cleanup_enabled=True,
        )
        assert isinstance(res, WorkspaceGCPreserved)
        assert res.reason_code == WORKSPACE_WITHIN_RETENTION


def test_classify_workspace_failed_no_work_expired():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=True):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/tmp"),
            now=datetime.now(UTC),
            cutoff_at=datetime.now(UTC) - timedelta(hours=24),
            default_policy=True,
            cleanup_enabled=True,
        )
        assert isinstance(res, WorkspaceGCCandidate)
        assert res.reason_code == FAILED_WORKSPACE_NO_WORK


def test_classify_workspace_failed_has_work_but_expired():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/tmp"),
            now=datetime.now(UTC),
            cutoff_at=datetime.now(UTC) - timedelta(hours=24),
            default_policy=True,
            cleanup_enabled=True,
        )
        assert isinstance(res, WorkspaceGCPreserved)


def test_completed_workspace_without_merged_pr_is_preserved():
    ws = Workspace(
        id="ws_unmerged",
        status=WorkspaceStatus.completed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=200),
        compose_project_name="proj",
        pr_url="http://github.com/pr/1",
        pr_number=1,
        pr_merge_sha=None,
    )
    res = _classify_workspace_for_gc(
        ws,
        work_dir=Path("/tmp"),
        now=datetime.now(UTC),
        cutoff_at=datetime.now(UTC) - timedelta(hours=24),
        default_policy=True,
        cleanup_enabled=True,
    )
    assert isinstance(res, WorkspaceGCPreserved)
    assert res.reason_code == "COMPLETED_PR_NOT_MERGED"


def test_completed_workspace_with_merged_pr_past_retention_is_candidate():
    ws = Workspace(
        id="ws_merged_old",
        status=WorkspaceStatus.completed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=200),
        compose_project_name="proj",
        pr_url="http://github.com/pr/2",
        pr_number=2,
        pr_merge_sha="a" * 40,
    )
    res = _classify_workspace_for_gc(
        ws,
        work_dir=Path("/tmp"),
        now=datetime.now(UTC),
        cutoff_at=datetime.now(UTC) - timedelta(hours=24),
        default_policy=True,
        cleanup_enabled=True,
    )
    assert isinstance(res, WorkspaceGCCandidate)
    assert res.reason_code == COMPLETED_PR_RETENTION_EXPIRED


def test_completed_workspace_with_merged_pr_within_retention_is_preserved():
    ws = Workspace(
        id="ws_merged_recent",
        status=WorkspaceStatus.completed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=2),
        compose_project_name="proj",
        pr_url="http://github.com/pr/3",
        pr_number=3,
        pr_merge_sha="b" * 40,
    )
    res = _classify_workspace_for_gc(
        ws,
        work_dir=Path("/tmp"),
        now=datetime.now(UTC),
        cutoff_at=datetime.now(UTC) - timedelta(hours=24),
        default_policy=True,
        cleanup_enabled=True,
    )
    assert isinstance(res, WorkspaceGCPreserved)
    assert res.reason_code == WORKSPACE_WITHIN_RETENTION
