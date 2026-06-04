"""Orphan AWF Docker resource and worktree detection."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from awf.service.orphan_resources import (
    WorkspaceIdView,
    build_orphan_resource_summary,
    empty_docker_scan,
    empty_worktree_scan,
    scan_docker_resources,
    scan_managed_worktrees,
)


class _Completed:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _jsonl(*rows: dict[str, str]) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


def _ok_view(
    *,
    active: set[str] | None = None,
    terminal: set[str] | None = None,
    retained: set[str] | None = None,
) -> WorkspaceIdView:
    return WorkspaceIdView(
        active_ids=frozenset(active or set()),
        terminal_ids=frozenset(terminal or set()),
        retained_ids=frozenset(retained or set()),
        available=True,
    )


def _run_for(
    *,
    containers: str = "",
    networks: str = "",
    volumes: str = "",
    fail_networks: bool = False,
) -> Any:
    def _run(args: list[str], **_kwargs: object) -> _Completed:
        if args[:3] == ["docker", "ps", "-a"]:
            return _Completed(stdout=containers)
        if args[:3] == ["docker", "network", "ls"]:
            if fail_networks:
                return _Completed(returncode=1, stderr="network list failed")
            return _Completed(stdout=networks)
        if args[:3] == ["docker", "volume", "ls"]:
            return _Completed(stdout=volumes)
        raise AssertionError(f"unexpected subprocess call: {args}")

    return _run


class _RecordingComposeTeardown:
    """Fake compose teardown for the readiness-driven reaper tests."""

    def __init__(self, result: Any | None = None) -> None:
        self.calls: list[tuple[str, Path, str]] = []
        self.remove_volumes_calls: list[bool] = []
        from awf.node.compose_manager import ComposeTeardownResult

        self._result = result or ComposeTeardownResult(
            status="succeeded", reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED"
        )

    async def __call__(
        self, project_name: str, compose_file: Path, workspace_id: str, remove_volumes: bool
    ) -> Any:
        self.calls.append((project_name, compose_file, workspace_id))
        self.remove_volumes_calls.append(remove_volumes)
        return self._result


def _orphan_summary_with_compose_and_worktree(tmp_path: Path, *, auto_cleanup_orphans: bool) -> Any:
    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_dead-agent-1",
                    "project": "awf_ws_dead",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            )
        ),
    )
    return build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),  # no rows -> both records are "missing" orphans
        auto_cleanup_orphans=auto_cleanup_orphans,
    )


@pytest.mark.unit
def test_reaper_flag_off_is_dry_run_and_noop(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _orphan_summary_with_compose_and_worktree(tmp_path, auto_cleanup_orphans=False)
    assert summary.cleanup_readiness.dry_run_only is True

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=False,
        )
    )

    assert result.enabled is False
    assert result.status == "disabled"
    assert teardown.calls == []
    assert (tmp_path / "git" / "worktrees" / "ws_dead").exists()


@pytest.mark.unit
def test_reaper_flag_on_reaps_compose_and_worktree(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _orphan_summary_with_compose_and_worktree(tmp_path, auto_cleanup_orphans=True)
    assert summary.cleanup_readiness.dry_run_only is False

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate reap mechanics from the row-less age guard
        )
    )

    assert result.status == "ok"
    assert len(teardown.calls) == 1
    assert teardown.calls[0][0] == "awf_ws_dead"
    assert teardown.calls[0][2] == "ws_dead"
    assert not (tmp_path / "git" / "worktrees" / "ws_dead").exists()
    reaped_kinds = sorted(outcome.kind for outcome in result.reaped)
    assert reaped_kinds == ["compose", "worktree"]
    payload = result.to_dict()
    assert payload["enabled"] is True
    assert payload["status"] == "ok"
    assert {entry["kind"] for entry in payload["reaped"]} == {"compose", "worktree"}


@pytest.mark.unit
def test_reaper_leaves_expected_and_unknown_records(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_live").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(active={"ws_live"}),
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "ok"
    assert result.reaped == ()
    assert teardown.calls == []
    assert (tmp_path / "git" / "worktrees" / "ws_live").exists()


@pytest.mark.unit
def test_reaper_skips_when_classification_unknown(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset(),
            available=False,
        ),
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "skipped"
    assert result.reason_code == "ORPHAN_REAP_SKIPPED_UNKNOWN"
    assert teardown.calls == []


@pytest.mark.unit
def test_reaper_skips_when_scanner_unavailable_with_partial_orphans(tmp_path: Path) -> None:
    """A scanner failure that still surfaces partial orphans must not drive reaping.

    When the container list succeeds but the network/volume scan fails,
    ``scan_docker_resources`` returns ``ok=False`` while still carrying the
    listed containers. Those containers would classify as orphans, but the
    inventory is incomplete, so the summary reports the degraded scan as
    ``unavailable``/report-only (the scanner-unavailable branch runs before the
    orphan-present branch) instead of advertising reaping for an inventory the
    reaper will not act on. The reaper then skips on the same incomplete
    inventory rather than tearing down stacks on guesswork.
    """
    from awf.service.orphan_resources import reap_classified_orphans

    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_dead-agent-1",
                    "project": "awf_ws_dead",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            ),
            fail_networks=True,
        ),
    )
    assert docker.ok is False
    assert docker.resources  # partial inventory: the container survived the failed scan
    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),  # no rows -> the listed container is a "missing" orphan
        auto_cleanup_orphans=True,
    )
    # The degraded scan is reported as report-only/unknown -- not blocked with
    # reaping advertised -- so operators are never told deletion is enabled for
    # an incomplete inventory.
    assert summary.status == "unavailable"
    assert summary.orphan_count == 0
    assert summary.cleanup_readiness.status == "unknown"
    assert summary.cleanup_readiness.dry_run_only is True

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,
        )
    )

    assert result.status == "skipped"
    assert result.reason_code == "ORPHAN_REAP_SKIPPED_UNKNOWN"
    assert teardown.calls == []


@pytest.mark.unit
def test_reaper_permission_denied_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from awf.service.gc_classify import PATH_DELETE_PERMISSION_DENIED
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
    )

    def _denied(kind: str, path: Path, *, work_dir: Path) -> tuple[bool, str | None, str | None]:
        return False, "permission denied", PATH_DELETE_PERMISSION_DENIED

    monkeypatch.setattr("awf.service.orphan_resources.build_and_delete_gc_path", _denied)

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate the permission-refusal path from the age guard
        )
    )

    assert result.status == "partial"
    assert result.reason_code == "ORPHAN_REAP_PARTIAL"
    assert len(result.errors) == 1
    assert result.errors[0].reason_code == PATH_DELETE_PERMISSION_DENIED


@pytest.mark.unit
def test_reaper_worktree_already_removed_is_idempotent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awf.service.gc_classify import PATH_ALREADY_REMOVED
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
    )

    def _vanished(kind: str, path: Path, *, work_dir: Path) -> tuple[bool, str | None, str | None]:
        return False, None, PATH_ALREADY_REMOVED

    monkeypatch.setattr("awf.service.orphan_resources.build_and_delete_gc_path", _vanished)

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate the idempotent-removal path from the age guard
        )
    )

    assert result.status == "ok"
    assert len(result.reaped) == 1
    assert result.reaped[0].status == "already_removed"


@pytest.mark.unit
def test_build_orphan_compose_teardown_invokes_manager() -> None:
    from awf.node.compose_manager import ComposeTeardownResult
    from awf.service.orphan_resources import build_orphan_compose_teardown

    class _FakeManager:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def teardown_project(
            self,
            *,
            project_name: str,
            compose_file: Path,
            workspace_id: str,
            remove_volumes: bool = True,
        ) -> ComposeTeardownResult:
            self.calls.append(
                {
                    "project_name": project_name,
                    "compose_file": compose_file,
                    "workspace_id": workspace_id,
                    "remove_volumes": remove_volumes,
                }
            )
            return ComposeTeardownResult(
                status="succeeded", reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED"
            )

    manager = _FakeManager()
    teardown = build_orphan_compose_teardown(manager)  # type: ignore[arg-type]
    result = asyncio.run(
        teardown("awf_ws_x", Path("/tmp/awf/compose/ws_x/compose.yml"), "ws_x", True)
    )

    assert result.ok
    assert manager.calls[0]["remove_volumes"] is True
    assert manager.calls[0]["project_name"] == "awf_ws_x"

    # The closure forwards the caller's per-workspace decision verbatim: a
    # retained-terminal stack is torn down without deleting its salvage volumes.
    asyncio.run(teardown("awf_ws_y", Path("/tmp/awf/compose/ws_y/compose.yml"), "ws_y", False))
    assert manager.calls[1]["remove_volumes"] is False


def _retained_terminal_runtime_summary(*, retained: bool) -> Any:
    """Summary for a terminal workspace with a leaked container plus a volume.

    When ``retained`` the workspace is still inside its retention window, so
    _classify keeps the volume ``expected`` (salvage evidence) while surfacing
    the live container as ``terminal``. When not retained, both the container
    and the volume classify ``terminal``.
    """

    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_done-agent-1",
                    "project": "awf_ws_done",
                    "service": "agent",
                    "state": "running",
                    "status": "Up",
                }
            ),
            volumes=_jsonl(
                {
                    "name": "awf_ws_done_pgdata",
                    "project": "awf_ws_done",
                    "driver": "local",
                    "scope": "local",
                }
            ),
        ),
    )
    view = _ok_view(terminal={"ws_done"}, retained={"ws_done"} if retained else None)
    return build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=empty_worktree_scan(),
        workspace_view=view,
        auto_cleanup_orphans=True,
    )


@pytest.mark.unit
def test_reaper_preserves_retained_terminal_salvage_volumes(tmp_path: Path) -> None:
    """Reaping a leaked container for a within-retention terminal workspace must
    not pass ``--volumes`` and delete the salvage volume _classify protected."""
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _retained_terminal_runtime_summary(retained=True)
    # The volume is preserved as salvage evidence; only the live container reaps.
    volume_record = next(record for record in summary.records if record.kind == "volume")
    assert volume_record.classification == "expected"
    assert volume_record.reason == "WORKSPACE_TERMINAL_WITHIN_RETENTION"

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "ok"
    assert teardown.calls == [
        ("awf_ws_done", tmp_path / "compose" / "ws_done" / "compose.yml", "ws_done")
    ]
    assert teardown.remove_volumes_calls == [False]


@pytest.mark.unit
def test_reaper_removes_volumes_for_fully_terminal_workspace(tmp_path: Path) -> None:
    """A terminal workspace past its retention window has its volume classified
    ``terminal``, so the stack is torn down with ``--volumes``."""
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _retained_terminal_runtime_summary(retained=False)
    volume_record = next(record for record in summary.records if record.kind == "volume")
    assert volume_record.classification == "terminal"

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "ok"
    assert teardown.remove_volumes_calls == [True]


@pytest.mark.unit
def test_reaper_compose_teardown_failure_is_loud(tmp_path: Path) -> None:
    from awf.node.compose_manager import ComposeTeardownResult
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _orphan_summary_with_compose_and_worktree(tmp_path, auto_cleanup_orphans=True)
    teardown = _RecordingComposeTeardown(
        ComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_UNAVAILABLE",
            error="daemon down",
        )
    )

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "partial"
    assert result.reason_code == "ORPHAN_REAP_PARTIAL"
    compose_errors = [outcome for outcome in result.errors if outcome.kind == "compose"]
    assert len(compose_errors) == 1
    assert compose_errors[0].reason_code == "DOCKER_UNAVAILABLE"
    assert compose_errors[0].error == "daemon down"


@pytest.mark.unit
def test_reaper_skips_young_missing_worktree(tmp_path: Path) -> None:
    """A just-created row-less worktree is left alone (possible in-flight provision)."""
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),  # no rows -> the worktree is classified "missing"
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=1.0,
        )
    )

    assert result.status == "ok"
    assert result.reaped == ()
    assert teardown.calls == []
    assert (tmp_path / "git" / "worktrees" / "ws_dead").exists()


@pytest.mark.unit
def test_reaper_reaps_aged_missing_worktree(tmp_path: Path) -> None:
    """An aged row-less worktree (older than the grace window) is reaped."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    old = 1_000_000.0
    os.utime(worktree, (old, old))
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            now=old + 7200.0,  # two hours after the worktree's mtime
            min_age_hours=1.0,
        )
    )

    assert result.status == "ok"
    reaped_kinds = [outcome.kind for outcome in result.reaped]
    assert reaped_kinds == ["worktree"]
    assert not worktree.exists()


@pytest.mark.unit
def test_reaper_reaps_terminal_worktree_without_age_guard(tmp_path: Path) -> None:
    """A fresh worktree backed by a terminal row is reaped despite the grace window."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(terminal={"ws_dead"}),  # terminal row -> not gated
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=168.0,
        )
    )

    assert result.status == "ok"
    assert [outcome.kind for outcome in result.reaped] == ["worktree"]
    assert not worktree.exists()


@pytest.mark.unit
def test_reaper_skips_young_missing_compose_when_dir_present(tmp_path: Path) -> None:
    """A row-less compose stack with a fresh on-disk compose dir is not torn down."""
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "compose" / "ws_dead").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_dead-agent-1",
                    "project": "awf_ws_dead",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            )
        ),
    )
    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=empty_worktree_scan(),
        workspace_view=_ok_view(),  # no rows -> the container is classified "missing"
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=1.0,
        )
    )

    assert result.status == "ok"
    assert teardown.calls == []
    assert result.reaped == ()
