"""Filesystem-vs-DB reconciler for orphaned per-workspace directories (WS-B2)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.companions import companion_worktree_id
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeTeardownResult
from awf.service.gc_classify import (
    PATH_DELETE_PERMISSION_DENIED,
    PATH_DELETED,
)
from awf.service.gc_reconcile import (
    ORPHAN_DIR_REAP_CAP_HIT,
    ORPHAN_DIR_REAP_DRY_RUN,
    ORPHAN_DIR_REAP_OK,
    ORPHAN_DIR_REAP_PARTIAL,
    OrphanDirTarget,
    reconcile_orphaned_workspace_dirs,
    scan_orphan_workspace_dirs,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


class _RecordingTeardown:
    """Fake compose teardown capturing calls and returning a fixed result."""

    def __init__(self, result: ComposeTeardownResult | None = None) -> None:
        self.calls: list[OrphanDirTarget] = []
        self._result = result or ComposeTeardownResult(
            status="succeeded", reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED"
        )

    async def __call__(self, target: OrphanDirTarget) -> ComposeTeardownResult:
        self.calls.append(target)
        return self._result


def _stamp(path: Path, *, age_seconds: float, now: float) -> None:
    moment = now - age_seconds
    os.utime(path, (moment, moment))


def _make_dir(path: Path, *, age_seconds: float | None = None, now: float | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if age_seconds is not None and now is not None:
        _stamp(path, age_seconds=age_seconds, now=now)
    return path


async def _create_workspace(session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title="reconcile candidate",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await session.commit()
        return workspace.id


def _seed_workspace_dirs(
    work_dir: Path, workspace_id: str, *, now: float, age_seconds: float = 0.0
):
    auth = _make_dir(work_dir / "auth" / workspace_id, age_seconds=age_seconds, now=now)
    worktree = _make_dir(
        work_dir / "git" / "worktrees" / workspace_id, age_seconds=age_seconds, now=now
    )
    compose = _make_dir(work_dir / "compose" / workspace_id)
    (compose / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    _stamp(compose, age_seconds=age_seconds, now=now)
    return auth, worktree, compose


@pytest.mark.usefixtures("engine")
async def test_reaps_row_less_dirs(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = 1_000_000.0
    auth, worktree, compose = _seed_workspace_dirs(tmp_path, "ws_orphan1", now=now)
    teardown = _RecordingTeardown()

    result = await reconcile_orphaned_workspace_dirs(
        session_factory,
        work_dir=tmp_path,
        compose_teardown=teardown,
        now=now,
        min_age_hours=0,
        execute=True,
    )

    assert result.status == "ok"
    assert result.reason_code == ORPHAN_DIR_REAP_OK
    assert result.reaped_count == 3
    assert not auth.exists()
    assert not worktree.exists()
    assert not compose.exists()
    assert len(teardown.calls) == 1
    assert teardown.calls[0].workspace_id == "ws_orphan1"
    assert result.compose_teardowns["ws_orphan1"]["status"] == "succeeded"
    assert all(outcome.reason_code == PATH_DELETED for outcome in result.reaped)


@pytest.mark.usefixtures("engine")
async def test_leaves_live_provisioning_young_and_companion_dirs(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = 2_000_000.0
    # A live workspace row protects all of its dirs, including a companion worktree.
    live_id = await _create_workspace(session_factory)
    live_auth, live_worktree, live_compose = _seed_workspace_dirs(tmp_path, live_id, now=now)
    companion_dir = _make_dir(
        tmp_path / "git" / "worktrees" / companion_worktree_id(live_id, "db"),
        age_seconds=10_000_000.0,
        now=now,
    )

    # A row-less but too-young dir must survive the grace window.
    young = _make_dir(tmp_path / "auth" / "ws_young", age_seconds=60.0, now=now)

    teardown = _RecordingTeardown()
    result = await reconcile_orphaned_workspace_dirs(
        session_factory,
        work_dir=tmp_path,
        compose_teardown=teardown,
        now=now,
        min_age_hours=24,
        execute=True,
    )

    assert result.reaped_count == 0
    assert result.orphan_count == 0
    assert teardown.calls == []
    assert live_auth.exists()
    assert live_worktree.exists()
    assert live_compose.exists()
    assert companion_dir.exists()
    assert young.exists()


@pytest.mark.usefixtures("engine")
async def test_permission_denied_is_loud(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 3_000_000.0
    _make_dir(tmp_path / "auth" / "ws_locked", age_seconds=0.0, now=now)

    def _denied(target: object, *, work_dir: Path) -> tuple[bool, str | None, str | None]:
        return False, "permission denied", PATH_DELETE_PERMISSION_DENIED

    monkeypatch.setattr("awf.service.gc_reconcile._delete_gc_path", _denied)

    result = await reconcile_orphaned_workspace_dirs(
        session_factory,
        work_dir=tmp_path,
        now=now,
        min_age_hours=0,
        execute=True,
    )

    assert result.status == "partial"
    assert result.reason_code == ORPHAN_DIR_REAP_PARTIAL
    assert result.reaped_count == 0
    assert len(result.errors) == 1
    assert result.errors[0].reason_code == PATH_DELETE_PERMISSION_DENIED
    assert result.errors[0].status == "failed"


@pytest.mark.usefixtures("engine")
async def test_bounded_and_logged_cap(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = 4_000_000.0
    for index in range(5):
        _make_dir(tmp_path / "auth" / f"ws_cap{index}", age_seconds=0.0, now=now)

    teardown = _RecordingTeardown()
    with structlog.testing.capture_logs() as captured:
        result = await reconcile_orphaned_workspace_dirs(
            session_factory,
            work_dir=tmp_path,
            compose_teardown=teardown,
            now=now,
            min_age_hours=0,
            limit=2,
            execute=True,
        )

    assert result.capped is True
    assert result.dropped_count == 3
    assert result.reaped_count == 2
    assert result.orphan_count == 5
    assert result.reason_code == ORPHAN_DIR_REAP_CAP_HIT
    cap_log = next(
        event for event in captured if event.get("event") == "gc.orphan_dir_reconcile.capped"
    )
    assert cap_log["reason_code"] == ORPHAN_DIR_REAP_CAP_HIT
    assert cap_log["dropped_count"] == 3


@pytest.mark.usefixtures("engine")
async def test_idempotent_second_run(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = 5_000_000.0
    _seed_workspace_dirs(tmp_path, "ws_idem", now=now)
    teardown = _RecordingTeardown()

    first = await reconcile_orphaned_workspace_dirs(
        session_factory,
        work_dir=tmp_path,
        compose_teardown=teardown,
        now=now,
        min_age_hours=0,
        execute=True,
    )
    assert first.status == "ok"
    assert first.reaped_count == 3

    second = await reconcile_orphaned_workspace_dirs(
        session_factory,
        work_dir=tmp_path,
        compose_teardown=teardown,
        now=now,
        min_age_hours=0,
        execute=True,
    )
    assert second.status == "ok"
    assert second.orphan_count == 0
    assert second.reaped_count == 0


@pytest.mark.usefixtures("engine")
async def test_dry_run_reports_without_deleting(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = 6_000_000.0
    auth, worktree, compose = _seed_workspace_dirs(tmp_path, "ws_dry", now=now)
    teardown = _RecordingTeardown()

    result = await reconcile_orphaned_workspace_dirs(
        session_factory,
        work_dir=tmp_path,
        compose_teardown=teardown,
        now=now,
        min_age_hours=0,
        execute=False,
    )

    assert result.status == "dry_run"
    assert result.reason_code == ORPHAN_DIR_REAP_DRY_RUN
    assert result.reaped_count == 0
    assert result.orphan_count == 3
    assert {target.workspace_id for target in result.planned} == {"ws_dry"}
    assert teardown.calls == []
    assert auth.exists()
    assert worktree.exists()
    assert compose.exists()


@pytest.mark.usefixtures("engine")
async def test_compose_teardown_failure_skips_filesystem_removal(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = 7_000_000.0
    _, _, compose = _seed_workspace_dirs(tmp_path, "ws_teardownfail", now=now)
    teardown = _RecordingTeardown(
        ComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_UNAVAILABLE",
            error="daemon down",
        )
    )

    result = await reconcile_orphaned_workspace_dirs(
        session_factory,
        work_dir=tmp_path,
        compose_teardown=teardown,
        now=now,
        min_age_hours=0,
        execute=True,
    )

    assert result.status == "partial"
    assert result.reason_code == ORPHAN_DIR_REAP_PARTIAL
    # The auth + worktree dirs (no compose dependency) still reap; compose is skipped.
    assert result.reaped_count == 2
    skipped = [outcome for outcome in result.errors if outcome.target.kind == "compose"]
    assert len(skipped) == 1
    assert skipped[0].status == "skipped"
    assert skipped[0].reason_code == "DOCKER_UNAVAILABLE"
    assert compose.exists()


def test_scan_is_pure_and_sorts_oldest_first(tmp_path: Path) -> None:
    now = 8_000_000.0
    _make_dir(tmp_path / "auth" / "ws_a", age_seconds=100.0, now=now)
    _make_dir(tmp_path / "auth" / "ws_b", age_seconds=5_000.0, now=now)

    targets, scanned, dropped = scan_orphan_workspace_dirs(
        tmp_path,
        frozenset(),
        now=now,
        min_age_hours=0,
        limit=10,
    )

    assert scanned == 2
    assert dropped == 0
    assert [target.workspace_id for target in targets] == ["ws_b", "ws_a"]


def test_scan_ignores_non_ws_and_non_directory_entries(tmp_path: Path) -> None:
    now = 9_000_000.0
    _make_dir(tmp_path / "auth" / "not_a_workspace", age_seconds=100.0, now=now)
    (tmp_path / "auth").mkdir(parents=True, exist_ok=True)
    stray_file = tmp_path / "auth" / "ws_stray_file"
    stray_file.write_text("x", encoding="utf-8")
    _stamp(stray_file, age_seconds=100.0, now=now)

    targets, scanned, dropped = scan_orphan_workspace_dirs(
        tmp_path,
        frozenset(),
        now=now,
        min_age_hours=0,
        limit=10,
    )

    assert targets == ()
    assert scanned == 0
    assert dropped == 0


def test_scan_skips_non_directory_root_without_crashing(tmp_path: Path) -> None:
    # A stray file occupying a root path must not raise NotADirectoryError;
    # ``is_dir()`` skips it the same way a missing root is skipped.
    now = 9_500_000.0
    (tmp_path / "auth").write_text("not a directory", encoding="utf-8")
    _make_dir(tmp_path / "compose" / "ws_live", age_seconds=100.0, now=now)

    targets, scanned, dropped = scan_orphan_workspace_dirs(
        tmp_path,
        frozenset(),
        now=now,
        min_age_hours=0,
        limit=10,
    )

    assert [target.workspace_id for target in targets] == ["ws_live"]
    assert scanned == 1
    assert dropped == 0


@pytest.mark.usefixtures("engine")
async def test_result_to_dict_round_trips(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = 10_000_000.0
    _seed_workspace_dirs(tmp_path, "ws_todict", now=now)
    teardown = _RecordingTeardown()

    result = await reconcile_orphaned_workspace_dirs(
        session_factory,
        work_dir=tmp_path,
        compose_teardown=teardown,
        now=now,
        min_age_hours=0,
        execute=True,
    )

    payload = result.to_dict()
    assert payload["status"] == "ok"
    assert payload["reason_code"] == ORPHAN_DIR_REAP_OK
    assert payload["reaped_count"] == 3
    assert "ws_todict" in payload["compose_teardowns"]
    assert isinstance(payload["reaped"], list)


@pytest.mark.usefixtures("engine")
async def test_already_removed_dir_is_idempotent_success(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 11_000_000.0
    _make_dir(tmp_path / "auth" / "ws_gone", age_seconds=0.0, now=now)

    def _vanished(target: object, *, work_dir: Path) -> tuple[bool, str | None, str | None]:
        # Simulate a concurrent reap removing the dir between scan and delete.
        return False, None, None

    monkeypatch.setattr("awf.service.gc_reconcile._delete_gc_path", _vanished)

    result = await reconcile_orphaned_workspace_dirs(
        session_factory,
        work_dir=tmp_path,
        now=now,
        min_age_hours=0,
        execute=True,
    )

    assert result.status == "ok"
    assert result.reaped_count == 1
    assert result.reaped[0].status == "already_removed"


@pytest.mark.usefixtures("engine")
async def test_gc_path_construction_runs_off_event_loop(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_gc_path`` (whose ``rglob`` walk is expensive) must not run on the loop.

    Regression: it was constructed on the event loop before the
    ``asyncio.to_thread`` deletion call, so the recursive ``_estimate_bytes``
    filesystem walk could stall the worker loop for seconds per orphan.
    """
    import threading

    from awf.service import gc_reconcile

    now = 12_000_000.0
    _make_dir(tmp_path / "auth" / "ws_offloop", age_seconds=0.0, now=now)

    loop_thread = threading.get_ident()
    observed_threads: list[int] = []
    real_gc_path = gc_reconcile._gc_path

    def _recording_gc_path(kind: str, path: Path):
        observed_threads.append(threading.get_ident())
        return real_gc_path(kind, path)

    monkeypatch.setattr("awf.service.gc_reconcile._gc_path", _recording_gc_path)

    result = await reconcile_orphaned_workspace_dirs(
        session_factory,
        work_dir=tmp_path,
        now=now,
        min_age_hours=0,
        execute=True,
    )

    assert result.reaped_count == 1
    assert len(observed_threads) == 1
    assert observed_threads[0] != loop_thread


async def test_build_default_compose_teardown_invokes_manager() -> None:
    from awf.service.gc_reconcile import build_default_compose_teardown

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
    teardown = build_default_compose_teardown(manager)  # type: ignore[arg-type]
    target = OrphanDirTarget(
        kind="compose",
        workspace_id="ws_default",
        path=Path("/tmp/awf/compose/ws_default"),
        age_seconds=1.0,
    )

    result = await teardown(target)

    assert result.ok
    assert manager.calls == [
        {
            "project_name": "awf_ws_default",
            "compose_file": Path("/tmp/awf/compose/ws_default/compose.yml"),
            "workspace_id": "ws_default",
            "remove_volumes": True,
        }
    ]
