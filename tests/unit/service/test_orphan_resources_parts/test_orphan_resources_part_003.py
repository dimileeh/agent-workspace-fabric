"""Sweep, row-less, and age-limit coverage for orphan resource reaping."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from awf.service.orphan_resources import (
    ORPHAN_REAP_DISABLED,
    ORPHAN_REAP_OK,
    ORPHAN_REAP_SKIPPED_UNKNOWN,
    WorkspaceIdView,
    build_orphan_resource_summary,
    empty_docker_scan,
    empty_worktree_scan,
    scan_docker_resources,
    scan_managed_worktrees,
)
from tests.unit.service.test_orphan_resources_parts.test_orphan_resources_part_002 import (
    _Completed,
    _jsonl,
    _ok_view,
    _orphan_summary_with_compose_and_worktree,
    _RecordingComposeTeardown,
    _run_for,
)


@pytest.mark.unit
def test_reaper_reaps_missing_volume_via_name_fallback_and_leaves_expected(
    tmp_path: Path,
) -> None:
    """A row-less ``missing`` volume + worktree are reclaimed; a live volume is left.

    The no-DB-record orphan class #637 targets: ``awf-ws_dead-postgres_data`` has lost its
    compose-project label after its workspace row was pruned, so the name fallback recovers
    ``ws_dead``, the reaper enumerates it, classifies it ``missing`` (no DB row), and tears
    the stack down with ``remove_volumes=True`` (the volume record is itself cleanup-ready)
    while also removing the orphaned worktree. A parallel ``ws_live`` volume whose row is
    active classifies ``expected`` and must not be touched.
    """
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            volumes=_jsonl(
                {"name": "awf-ws_dead-postgres_data", "project": "", "driver": "local"},
                {
                    "name": "awf-ws_live-postgres_data",
                    "project": "awf-ws_live",
                    "driver": "local",
                },
            )
        ),
    )
    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(active={"ws_live"}),  # ws_dead has no row -> missing
        auto_cleanup_orphans=True,
        reaper_available=True,
    )
    dead_volume = next(
        record
        for record in summary.records
        if record.kind == "volume" and record.workspace_id == "ws_dead"
    )
    assert dead_volume.classification == "missing"
    assert dead_volume.compose_project == "awf-ws_dead"
    live_volume = next(
        record
        for record in summary.records
        if record.kind == "volume" and record.workspace_id == "ws_live"
    )
    assert live_volume.classification == "expected"

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate the reap from the row-less age guard
        )
    )

    assert result.status == "ok"
    # Only the dead stack is torn down, and with --volumes (its volume is cleanup-ready);
    # the live workspace's expected volume is never touched.
    assert teardown.calls == [
        ("awf-ws_dead", tmp_path / "compose" / "ws_dead" / "compose.yml", "ws_dead")
    ]
    assert teardown.remove_volumes_calls == [True]
    # The recovered label-less volume name is forwarded so the label-scoped teardown
    # can remove it by name -- without it the volume would leak while reported reaped
    # (PRRT_kwDOSJAM6s6LCiLk). The live workspace's expected volume contributes no name.
    assert teardown.fallback_volume_names_calls == [("awf-ws_dead-postgres_data",)]
    assert not (tmp_path / "git" / "worktrees" / "ws_dead").exists()
    assert sorted(outcome.kind for outcome in result.reaped) == ["compose", "worktree"]


@pytest.mark.unit
def test_reaper_compose_teardown_failure_is_loud(tmp_path: Path) -> None:
    """Verify reaper compose teardown failure is loud."""
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
        reaper_available=True,
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
        reaper_available=True,
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
def test_reaper_limit_bounds_to_oldest_workspaces_first(tmp_path: Path) -> None:
    """``--limit`` bounds the row-less sweep to the N oldest distinct workspaces.

    The DB-row terminal reaper already honours ``--limit`` oldest-first; the additive
    row-less orphan sweep must too, so ``awf service gc --execute --limit 1`` cannot tear
    down every aged row-less orphan in one pass (PRRT_kwDOSJAM6s6LCCJZ). "Oldest" is the
    on-disk anchor mtime — the same signal the age gate reads — since a row-less orphan
    has no DB ``updated_at`` to sort on.
    """
    from awf.service.orphan_resources import reap_classified_orphans

    worktrees = tmp_path / "git" / "worktrees"
    old = worktrees / "ws_old"
    new = worktrees / "ws_new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    os.utime(old, (1_000_000.0, 1_000_000.0))
    os.utime(new, (2_000_000.0, 2_000_000.0))
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),  # no rows -> both worktrees classify "missing"
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate the limit bound from the row-less age guard
            limit=1,
        )
    )

    assert result.status == "ok"
    assert [outcome.workspace_id for outcome in result.reaped] == ["ws_old"]
    assert not old.exists()
    assert new.exists()  # the newer workspace is left for a later batch


@pytest.mark.unit
def test_reaper_limit_reaps_selected_workspace_records_together(tmp_path: Path) -> None:
    """Bounding by DISTINCT workspace never half-reaps a stack (PRRT_kwDOSJAM6s6LCCJZ).

    A workspace surfaces several records (a compose stack + its worktree). Under
    ``--limit 1`` the single selected (oldest) workspace must be reaped as a unit — both
    its compose teardown and its worktree — while the un-selected newer workspace is left
    entirely intact.
    """
    from awf.service.orphan_resources import reap_classified_orphans

    worktrees = tmp_path / "git" / "worktrees"
    full = worktrees / "ws_full"
    newer = worktrees / "ws_new"
    full.mkdir(parents=True)
    newer.mkdir(parents=True)
    os.utime(full, (1_000_000.0, 1_000_000.0))
    os.utime(newer, (2_000_000.0, 2_000_000.0))
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_full-agent-1",
                    "project": "awf_ws_full",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            )
        ),
    )
    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),  # no rows -> every record is a "missing" orphan
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,
            limit=1,
        )
    )

    assert result.status == "ok"
    assert [call[2] for call in teardown.calls] == ["ws_full"]
    assert {(outcome.kind, outcome.workspace_id) for outcome in result.reaped} == {
        ("compose", "ws_full"),
        ("worktree", "ws_full"),
    }
    assert not full.exists()
    assert newer.exists()


@pytest.mark.unit
def test_reaper_limit_above_workspace_count_reaps_all(tmp_path: Path) -> None:
    """A ``--limit`` larger than the distinct-workspace count clamps to reaping all."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktrees = tmp_path / "git" / "worktrees"
    (worktrees / "ws_a").mkdir(parents=True)
    (worktrees / "ws_b").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
        reaper_available=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,
            limit=5,
        )
    )

    assert result.status == "ok"
    assert {outcome.workspace_id for outcome in result.reaped} == {"ws_a", "ws_b"}
    assert not (worktrees / "ws_a").exists()
    assert not (worktrees / "ws_b").exists()


class _SessionScope:
    """Async context manager test double for orphan sweep DB sessions."""

    def __init__(self, session: object | None = None, error: SQLAlchemyError | None = None) -> None:
        """Store the fake session or entry error to expose during context entry."""
        self._session = session or object()
        self._error = error

    async def __aenter__(self) -> object:
        """Return the fake session or raise the configured entry error."""
        if self._error is not None:
            raise self._error
        return self._session

    async def __aexit__(self, *args: object) -> None:
        """Leave the fake session scope without suppressing exceptions."""


@pytest.mark.unit
def test_sweep_classified_orphans_scans_classifies_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A classified sweep scans resources, loads DB state, and reaps matches."""
    from awf.service.orphan_resources import sweep_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    docker_hosts: list[str] = []

    def _run(args: list[str], **kwargs: object) -> _Completed:
        """Capture Docker host wiring while returning one orphan container."""
        env = kwargs["env"]
        assert isinstance(env, dict)
        docker_hosts.append(str(env["DOCKER_HOST"]))
        return _run_for(
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
        )(args, **kwargs)

    monkeypatch.setattr(
        "awf.service.orphan_resources.session_scope",
        lambda _factory: _SessionScope(),
    )

    async def _workspace_view(_session: object, **kwargs: object) -> WorkspaceIdView:
        """Return a terminal workspace view for the sweep under test."""
        assert kwargs["min_retention_hours"] == 72.0
        return _ok_view()

    monkeypatch.setattr(
        "awf.service.orphan_resources.workspace_id_view_from_session",
        _workspace_view,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        sweep_classified_orphans(
            object(),  # type: ignore[arg-type]
            work_dir=tmp_path,
            docker_host="unix:///test-docker.sock",
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,
            min_retention_hours=72.0,
            run_subprocess=_run,
        )
    )

    assert result.status == "ok"
    assert result.reason_code == ORPHAN_REAP_OK
    assert teardown.calls == [
        ("awf_ws_dead", tmp_path / "compose" / "ws_dead" / "compose.yml", "ws_dead")
    ]
    assert not (tmp_path / "git" / "worktrees" / "ws_dead").exists()
    assert set(docker_hosts) == {"unix:///test-docker.sock"}


@pytest.mark.unit
def test_sweep_classified_orphans_forwards_now_anchor_to_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep forwards its ``now`` anchor into the reap (PRRT_kwDOSJAM6s6LCs9R).

    The on-demand ``service gc`` path freezes the row-less orphan grace at the API's request time by
    threading ``now`` down the call chain. The sweep must hand that anchor to
    :func:`reap_classified_orphans` (whose ``_missing_record_is_aged`` measures age against it)
    rather than dropping it and letting the reaper fall back to the worker's claim-time
    ``time.time()``.
    """
    import awf.service.orphan_resources as orphan_resources

    monkeypatch.setattr(orphan_resources, "session_scope", lambda _factory: _SessionScope())

    async def _workspace_view(_session: object, **_kwargs: object) -> WorkspaceIdView:
        """Test helper for workspace view."""
        return _ok_view()

    monkeypatch.setattr(orphan_resources, "workspace_id_view_from_session", _workspace_view)

    captured: dict[str, object] = {}

    async def _fake_reap(_summary: object, **kwargs: object) -> object:
        """Test helper for fake reap."""
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(orphan_resources, "reap_classified_orphans", _fake_reap)

    asyncio.run(
        orphan_resources.sweep_classified_orphans(
            object(),  # type: ignore[arg-type]
            work_dir=tmp_path,
            docker_host="unix:///test-docker.sock",
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
            run_subprocess=_run_for(),
            now=1_700_000_000.0,
        )
    )

    assert captured["now"] == 1_700_000_000.0


@pytest.mark.unit
def test_sweep_classified_orphans_disabled_skips_scans_and_workspace_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled classified sweep exits before scans or DB classification."""
    import awf.service.orphan_resources as orphan_resources

    calls: list[str] = []

    def _unexpected_docker_scan(**_kwargs: object) -> Any:
        """Fail if the disabled sweep scans Docker resources."""
        calls.append("docker")
        raise AssertionError("disabled sweep should not scan Docker resources")

    def _unexpected_worktree_scan(_work_dir: Path | str) -> Any:
        """Fail if the disabled sweep scans worktrees."""
        calls.append("worktree")
        raise AssertionError("disabled sweep should not scan managed worktrees")

    def _unexpected_session_scope(_factory: object) -> Any:
        """Fail if the disabled sweep opens a DB session."""
        calls.append("workspace_view")
        raise AssertionError("disabled sweep should not load workspace view")

    monkeypatch.setattr(orphan_resources, "scan_docker_resources", _unexpected_docker_scan)
    monkeypatch.setattr(orphan_resources, "scan_managed_worktrees", _unexpected_worktree_scan)
    monkeypatch.setattr(orphan_resources, "session_scope", _unexpected_session_scope)

    result = asyncio.run(
        orphan_resources.sweep_classified_orphans(
            object(),  # type: ignore[arg-type]
            work_dir=tmp_path,
            docker_host="unix:///test-docker.sock",
            compose_teardown=_RecordingComposeTeardown(),
            enabled=False,
        )
    )

    assert result.enabled is False
    assert result.status == "disabled"
    assert result.reason_code == ORPHAN_REAP_DISABLED
    assert result.reaped == ()
    assert result.errors == ()
    assert calls == []


@pytest.mark.unit
def test_sweep_classified_orphans_starts_scanners_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker and worktree scans are started concurrently during a sweep."""
    import awf.service.orphan_resources as orphan_resources

    started: set[str] = set()
    sequential_starts: list[str] = []
    both_scanners_started = asyncio.Event()

    def _docker_scan(**_kwargs: object) -> Any:
        """Return an empty Docker scan once asyncio.to_thread invokes it."""
        return empty_docker_scan()

    def _worktree_scan(work_dir: Path | str) -> Any:
        """Return an empty worktree scan for the expected work directory."""
        assert work_dir == tmp_path
        return empty_worktree_scan()

    async def _to_thread(func: Any, /, *args: object, **kwargs: object) -> Any:
        """Emulate to_thread while detecting whether both scanners are started."""
        if func in {_docker_scan, _worktree_scan}:
            scanner = "docker" if func is _docker_scan else "worktree"
            started.add(scanner)
            if len(started) == 2:
                both_scanners_started.set()
            try:
                await asyncio.wait_for(both_scanners_started.wait(), timeout=0.25)
            except TimeoutError:
                sequential_starts.append(scanner)
            return func(*args, **kwargs)
        return func(*args, **kwargs)

    monkeypatch.setattr(orphan_resources.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(orphan_resources, "scan_docker_resources", _docker_scan)
    monkeypatch.setattr(orphan_resources, "scan_managed_worktrees", _worktree_scan)
    monkeypatch.setattr(
        orphan_resources,
        "session_scope",
        lambda _factory: _SessionScope(),
    )

    async def _workspace_view(_session: object, **_kwargs: object) -> WorkspaceIdView:
        """Return an empty available workspace view for the concurrency test."""
        return _ok_view()

    monkeypatch.setattr(
        orphan_resources,
        "workspace_id_view_from_session",
        _workspace_view,
    )

    result = asyncio.run(
        orphan_resources.sweep_classified_orphans(
            object(),  # type: ignore[arg-type]
            work_dir=tmp_path,
            docker_host="unix:///test-docker.sock",
            compose_teardown=_RecordingComposeTeardown(),
            enabled=True,
        )
    )

    assert result.status == "ok"
    assert started == {"docker", "worktree"}
    assert sequential_starts == []


@pytest.mark.unit
def test_sweep_classified_orphans_skips_when_workspace_view_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB classification failure skips reaping unknown orphan resources."""
    from awf.service.orphan_resources import sweep_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(
        "awf.service.orphan_resources.session_scope",
        lambda _factory: _SessionScope(error=SQLAlchemyError("db unavailable")),
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        sweep_classified_orphans(
            object(),  # type: ignore[arg-type]
            work_dir=tmp_path,
            docker_host="unix:///test-docker.sock",
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,
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
    )

    assert result.status == "skipped"
    assert result.reason_code == ORPHAN_REAP_SKIPPED_UNKNOWN
    assert teardown.calls == []
    assert worktree.exists()


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
        reaper_available=True,
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
        reaper_available=True,
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
