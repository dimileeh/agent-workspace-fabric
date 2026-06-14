"""Worker-runtime wiring for the terminal-workspace auth-dir GC reaper (#513).

Split out of ``test_worker.py`` to keep that module under the first-party file
line limit (``test_first_party_code_files_stay_under_line_limit``). Shares the
``_settings`` / ``_in_process_merge_coordinator`` builders with the parent
module.

The terminal-workspace GC (``run_service_workspace_gc``) is the same engine the
``POST /v1/service/gc`` route calls, but that route runs in the capability-less
API container and skips every per-workspace auth dir (the overlay
unmount-before-remove cannot be verified without CAP_SYS_ADMIN). Only the worker
holds CAP_SYS_ADMIN, so these tests prove (a) the worker is wired with a closure
that drives the real GC and (b) the kill-switch/interval forward end to end into
``WorkerConfig`` — guarding the #511-class regression — and (c) the closure
actually removes a terminal-workspace auth dir while preserving a failed one.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeTeardownResult
from awf.service import worker as worker_mod
from tests.unit.service.test_worker import (
    _in_process_merge_coordinator,
    _settings,
)


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


class _AnyInit:
    """A no-op stand-in for the node/runtime classes the runtime constructs."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


class _FakeComposeManager:
    """ComposeManager stand-in whose teardown always succeeds (no real docker)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def teardown_project(
        self,
        *,
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        remove_volumes: bool = True,
    ) -> ComposeTeardownResult:
        return ComposeTeardownResult(
            status="succeeded", reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED"
        )


def _patch_runtime_constructors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out the heavyweight node/runtime classes ``build_worker_runtime`` builds."""
    for name in (
        "AsyncioSubprocessRunner",
        "LogStore",
        "ValidationRunner",
        "PullRequestCreator",
        "BranchOpenPullRequestResolver",
        "GitManager",
        "ServiceAuthMountResolver",
        "LocalSecretLeaseMountResolver",
        "ComposeStackLauncher",
        "Provisioner",
        "WorkspaceExecutor",
        "CcusageCollector",
    ):
        monkeypatch.setattr(worker_mod, name, _AnyInit)
    monkeypatch.setattr(worker_mod, "ComposeManager", _FakeComposeManager)
    monkeypatch.setattr(
        worker_mod, "_merge_coordinator_for_database_url", _in_process_merge_coordinator
    )
    monkeypatch.setattr(worker_mod, "_companion_image_builder_for", lambda *_a, **_k: None)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(worker_mod, "_apply_service_git_environment", lambda _env: None)
    monkeypatch.setattr(worker_mod, "build_default_compose_teardown", lambda _manager: object())
    monkeypatch.setattr(worker_mod, "build_orphan_compose_teardown", lambda _manager: object())


@pytest.mark.unit
def test_build_worker_runtime_wires_terminal_gc_reaper_and_forwards_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The worker is wired with a terminal-GC reaper and the kill-switch/interval forward (#513).

    Asserts the closure is wired and that ``terminal_workspace_gc_enabled`` /
    ``terminal_workspace_gc_scan_interval_seconds`` flow ``Settings`` →
    ``ServiceSettings`` → ``WorkerConfig`` (the forward #511 originally dropped for
    the claude-base interval). A non-default interval proves it is the settings
    value, not a hard-coded default.
    """
    created: dict[str, Any] = {}

    class _ControlWorker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["terminal_gc_reaper"] = kwargs["terminal_gc_reaper"]
            created["worker_config"] = kwargs["config"]

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: object())
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: object())
    _patch_runtime_constructors(monkeypatch)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)

    settings = dataclasses.replace(
        _settings(tmp_path),
        terminal_workspace_gc_enabled=True,
        terminal_workspace_gc_scan_interval_seconds=1800.0,
    )

    worker_mod.build_worker_runtime(settings)

    reaper = created["terminal_gc_reaper"]
    assert reaper is not None
    assert callable(reaper)
    config = created["worker_config"]
    assert config.terminal_workspace_gc_enabled is settings.terminal_workspace_gc_enabled
    assert (
        config.terminal_workspace_gc_scan_interval_seconds
        == settings.terminal_workspace_gc_scan_interval_seconds
    )


async def _terminal_workspace_with_auth_overlay(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    work_dir: Path,
    status: WorkspaceStatus,
    updated_at: datetime,
) -> tuple[str, Path]:
    """Seed a terminal workspace plus a Claude auth-overlay scratch dir.

    The auth dir holds an overlay ``upper`` (the writable scratch) with no live
    ``merged`` mount and no ``.overlay-unmounted`` marker, so the GC's
    unmount-before-remove path is exercised: a capability-less probe would refuse
    (``OverlayUnmountUnverifiableError``), but the worker's CAP_SYS_ADMIN vantage
    verifies there is nothing to release and removal proceeds.
    """
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title="gc candidate",
            task_prompt="p",
            agent="claude",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        if status == WorkspaceStatus.completed:
            workspace.pr_url = "https://github.com/example/repo/pull/7"
            workspace.pr_number = 7
            workspace.pr_merge_sha = "a" * 40
        workspace_id = workspace.id
        await session.commit()

    upper = work_dir / "auth" / workspace_id / "claude" / "upper"
    upper.mkdir(parents=True)
    (upper / "scratch").write_text("y" * 256, encoding="utf-8")
    return workspace_id, (work_dir / "auth" / workspace_id)


@pytest.mark.unit
async def test_worker_terminal_gc_reaper_removes_auth_dir_and_preserves_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end: the wired closure removes a completed auth dir and preserves a failed one.

    Drives the real ``run_service_workspace_gc`` from the worker closure against a
    temp ``work_dir`` seeded with a completed (reaped) and a failed (preserved)
    terminal workspace, each with a Claude overlay scratch. A forced True
    capability probe stands in for the worker's CAP_SYS_ADMIN, so the
    unmount-before-remove succeeds instead of raising — exactly the gap the API
    path cannot close (#513). The ``preserves_failed_workspaces=True`` policy is
    inherited from the GC, so the failed auth dir must survive.
    """
    created: dict[str, Any] = {}

    class _ControlWorker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["terminal_gc_reaper"] = kwargs["terminal_gc_reaper"]

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: object())
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    _patch_runtime_constructors(monkeypatch)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)
    # The worktree remover runs ``git worktree remove`` — stub it so the GC does
    # not shell out for the (non-existent) candidate worktrees.
    from unittest.mock import AsyncMock

    from awf.service.gc import WorkspaceGCWorktreeRemoveResult

    monkeypatch.setattr(
        "awf.service.gc._default_worktree_remover",
        AsyncMock(
            return_value=WorkspaceGCWorktreeRemoveResult(
                status="succeeded", reason_code="WORKTREE_REMOVE_SUCCEEDED"
            )
        ),
    )
    # Force a trustworthy capability probe so the no-overlay test host does not
    # raise ``OverlayUnmountUnverifiableError`` (mirrors the worker's CAP_SYS_ADMIN
    # vantage). The default ``teardown_workspace_auth_overlay`` probe resolves
    # ``_has_cap_sys_admin`` from ``awf.node.auth_mounts_claude``.
    monkeypatch.setattr("awf.node.auth_mounts_claude._has_cap_sys_admin", lambda: True)

    # Keep the focus on per-workspace auth dirs: the same-pass claude-base reap is
    # covered by #509's tests, and disabling it avoids the host ``~/.claude``
    # signature machinery here.
    settings = dataclasses.replace(
        _settings(tmp_path, completed_workspace_retention_hours=24.0),
        claude_base_gc_enabled=False,
        # The companion-image prune shells out to docker; off keeps the run focused
        # on the auth-dir reclaim (and avoids a spurious ``partial`` on a host with
        # no docker).
        companion_image_cache_enabled=False,
    )
    work_dir = Path(settings.work_dir).resolve()
    old = datetime.now(UTC) - timedelta(hours=200)
    completed_id, completed_auth = await _terminal_workspace_with_auth_overlay(
        session_factory,
        work_dir=work_dir,
        status=WorkspaceStatus.completed,
        updated_at=old,
    )
    _failed_id, failed_auth = await _terminal_workspace_with_auth_overlay(
        session_factory,
        work_dir=work_dir,
        status=WorkspaceStatus.failed,
        updated_at=old,
    )
    assert completed_auth.is_dir()
    assert failed_auth.is_dir()

    worker_mod.build_worker_runtime(settings)
    reaper = created["terminal_gc_reaper"]

    report = await reaper()

    # The completed workspace's auth dir is reclaimed; the failed one is preserved.
    assert not completed_auth.exists()
    assert failed_auth.is_dir()
    assert report["status"] == "succeeded"
    assert str(completed_auth) in report["deleted_paths"]
    assert completed_id in {c["workspace_id"] for c in report["candidates"]}


@pytest.mark.unit
async def test_worker_terminal_gc_reaper_reaps_cancelled_and_destroyed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The wired closure reaps cancelled/destroyed auth dirs the default policy skips (#513).

    ``plan_terminal_workspace_gc``'s conservative default policy only classifies
    completed/failed/superseded rows (guarded by
    ``test_default_gc_policy_ignores_non_pr_terminal_and_unknown_statuses``), so a
    single default-policy sweep never even looks at ``cancelled``/``destroyed``
    workspaces and their ~1.7 GB auth dirs leak — the exact case #513 set out to
    fix. The worker therefore runs a second, explicit ``include_statuses`` pass for
    those discarded statuses (they carry no merged-PR work to preserve, so an
    age-based sweep is correct) while leaving the first pass's completed-merged /
    failed-preservation nuance untouched. This proves both discarded dirs are
    removed and the failed one still survives.
    """
    created: dict[str, Any] = {}

    class _ControlWorker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["terminal_gc_reaper"] = kwargs["terminal_gc_reaper"]

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: object())
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    _patch_runtime_constructors(monkeypatch)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)
    from unittest.mock import AsyncMock

    from awf.service.gc import WorkspaceGCWorktreeRemoveResult

    monkeypatch.setattr(
        "awf.service.gc._default_worktree_remover",
        AsyncMock(
            return_value=WorkspaceGCWorktreeRemoveResult(
                status="succeeded", reason_code="WORKTREE_REMOVE_SUCCEEDED"
            )
        ),
    )
    monkeypatch.setattr("awf.node.auth_mounts_claude._has_cap_sys_admin", lambda: True)

    settings = dataclasses.replace(
        _settings(tmp_path, completed_workspace_retention_hours=24.0),
        claude_base_gc_enabled=False,
        companion_image_cache_enabled=False,
    )
    work_dir = Path(settings.work_dir).resolve()
    old = datetime.now(UTC) - timedelta(hours=200)
    cancelled_id, cancelled_auth = await _terminal_workspace_with_auth_overlay(
        session_factory,
        work_dir=work_dir,
        status=WorkspaceStatus.cancelled,
        updated_at=old,
    )
    destroyed_id, destroyed_auth = await _terminal_workspace_with_auth_overlay(
        session_factory,
        work_dir=work_dir,
        status=WorkspaceStatus.destroyed,
        updated_at=old,
    )
    _failed_id, failed_auth = await _terminal_workspace_with_auth_overlay(
        session_factory,
        work_dir=work_dir,
        status=WorkspaceStatus.failed,
        updated_at=old,
    )
    assert cancelled_auth.is_dir()
    assert destroyed_auth.is_dir()
    assert failed_auth.is_dir()

    worker_mod.build_worker_runtime(settings)
    reaper = created["terminal_gc_reaper"]

    report = await reaper()

    # Both discarded-status auth dirs are reclaimed; the failed one is preserved.
    assert not cancelled_auth.exists()
    assert not destroyed_auth.exists()
    assert failed_auth.is_dir()
    assert report["status"] == "succeeded"
    deleted = set(report["deleted_paths"])
    assert str(cancelled_auth) in deleted
    assert str(destroyed_auth) in deleted
    candidate_ids = {c["workspace_id"] for c in report["candidates"]}
    assert {cancelled_id, destroyed_id} <= candidate_ids


class _FakeGCPlan:
    """Stand-in for ``WorkspaceGCResult.plan`` exposing only ``candidates``."""

    def __init__(self, candidate_count: int) -> None:
        self.candidates = [object()] * candidate_count


class _FakeGCResult:
    """Minimal ``run_service_workspace_gc`` return capturing the selected count."""

    def __init__(self, candidate_count: int) -> None:
        self.plan = _FakeGCPlan(candidate_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
            "deleted_paths": [],
            "candidates": [],
            "delete_errors": [],
            "preserved_count": 0,
        }


async def _limits_for_two_pass_gc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_limit: int,
    first_pass_selected: int,
) -> list[int | None]:
    """Build the reaper, run it, and return the ``limit`` passed to each GC pass."""
    created: dict[str, Any] = {}

    class _ControlWorker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["terminal_gc_reaper"] = kwargs["terminal_gc_reaper"]

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: object())
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    _patch_runtime_constructors(monkeypatch)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)

    limits: list[int | None] = []

    async def _fake_gc(_session_factory: object, **kwargs: Any) -> _FakeGCResult:
        limits.append(kwargs.get("limit"))
        # Only the first (default-policy) pass reports selected candidates; the
        # second pass's budget is what we are asserting on.
        return _FakeGCResult(first_pass_selected if len(limits) == 1 else 0)

    monkeypatch.setattr(worker_mod, "run_service_workspace_gc", _fake_gc)

    settings = dataclasses.replace(
        _settings(tmp_path),
        workspace_cleanup_batch_limit=batch_limit,
    )
    worker_mod.build_worker_runtime(settings)
    reaper = created["terminal_gc_reaper"]
    await reaper()
    return limits


@pytest.mark.unit
async def test_worker_terminal_gc_shares_batch_budget_across_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The discarded-status pass gets the batch budget minus the first pass's selections.

    Both passes share one ``workspace_cleanup_batch_limit`` budget, so the combined
    sweep cannot reclaim more than the configured guard — otherwise a limit of N
    could delete up to ~2N candidates in one cycle, breaking the per-batch GC
    invariant.
    """
    limits = await _limits_for_two_pass_gc(
        monkeypatch,
        tmp_path,
        session_factory,
        batch_limit=5,
        first_pass_selected=3,
    )

    assert limits == [5, 2]


@pytest.mark.unit
async def test_worker_terminal_gc_exhausted_budget_makes_second_pass_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A first pass that fills the batch budget leaves the discarded pass a no-op (``limit=0``).

    Without clamping, ``batch_limit - first_pass_selected`` would go negative; the
    second pass must receive ``0`` so it selects nothing rather than a negative limit.
    """
    limits = await _limits_for_two_pass_gc(
        monkeypatch,
        tmp_path,
        session_factory,
        batch_limit=4,
        first_pass_selected=6,
    )

    assert limits == [4, 0]


@pytest.mark.unit
async def test_worker_terminal_gc_reaper_honours_operator_scope_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Override ``min_age_hours``/``limit`` flow into both GC passes (#590).

    An on-demand ``awf service gc --execute`` delegation hands the worker the
    operator-resolved scope; the capability-gated auth-overlay/claude-base reap must
    run at that scope (here ``min_age_hours=1.0``, ``limit=9``) rather than the
    worker's configured retention/batch defaults, so the two paths stay in lockstep.
    The first pass selects 2 candidates, so the shared-budget second pass receives
    ``9 - 2 = 7`` while both passes use the override retention.
    """
    created: dict[str, Any] = {}

    class _ControlWorker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["terminal_gc_reaper"] = kwargs["terminal_gc_reaper"]

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: object())
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    _patch_runtime_constructors(monkeypatch)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)

    calls: list[tuple[float | None, int | None]] = []

    async def _fake_gc(_session_factory: object, **kwargs: Any) -> _FakeGCResult:
        calls.append((kwargs.get("min_age_hours"), kwargs.get("limit")))
        return _FakeGCResult(2 if len(calls) == 1 else 0)

    monkeypatch.setattr(worker_mod, "run_service_workspace_gc", _fake_gc)

    settings = dataclasses.replace(
        _settings(tmp_path, completed_workspace_retention_hours=24.0),
        workspace_cleanup_batch_limit=5,
    )
    worker_mod.build_worker_runtime(settings)
    reaper = created["terminal_gc_reaper"]

    await reaper(min_age_hours=1.0, limit=9)

    # Both passes use the override retention; the second pass shares the override
    # budget (9 - 2 selected = 7), not the configured default of 5.
    assert calls == [(1.0, 9), (1.0, 7)]


@pytest.mark.unit
def test_combine_terminal_gc_reports_concatenates_and_sums() -> None:
    """A clean default pass + clean discarded pass fold into one ``succeeded`` summary."""
    default_report: dict[str, Any] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_paths": ["/a"],
        "candidates": [{"workspace_id": "ws_completed"}],
        "delete_errors": [],
        "preserved_count": 1,
        "deleted_path_count": 1,
        "candidate_count": 1,
    }
    discarded_report: dict[str, Any] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_paths": ["/b", "/c"],
        "candidates": [{"workspace_id": "ws_cancelled"}, {"workspace_id": "ws_destroyed"}],
        "delete_errors": [],
        "preserved_count": 2,
        "deleted_path_count": 2,
        "candidate_count": 2,
    }

    combined = worker_mod._combine_terminal_gc_reports(default_report, discarded_report)

    assert combined["status"] == "succeeded"
    assert combined["reason_code"] == "CLEANUP_EXECUTION_SUCCEEDED"
    assert combined["deleted_paths"] == ["/a", "/b", "/c"]
    assert combined["deleted_path_count"] == 3
    assert combined["candidate_count"] == 3
    assert combined["preserved_count"] == 3


@pytest.mark.unit
def test_combine_terminal_gc_reports_partial_in_either_pass_wins() -> None:
    """A ``partial`` from the discarded pass is never masked behind the default pass.

    The discarded-status pass leaked disk it could not reclaim, so the merged
    summary must report ``partial`` (and the partial reason code) even though the
    default pass reported a clean success.
    """
    from awf.service.gc import CLEANUP_EXECUTION_PARTIAL

    default_report: dict[str, Any] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_paths": [],
        "candidates": [],
        "delete_errors": [],
        "preserved_count": 0,
    }
    discarded_report: dict[str, Any] = {
        "status": "partial",
        "reason_code": CLEANUP_EXECUTION_PARTIAL,
        "deleted_paths": [],
        "candidates": [],
        "delete_errors": [{"kind": "auth", "error": "boom"}],
        "preserved_count": 0,
    }

    combined = worker_mod._combine_terminal_gc_reports(default_report, discarded_report)

    assert combined["status"] == "partial"
    assert combined["reason_code"] == CLEANUP_EXECUTION_PARTIAL
    assert combined["delete_errors"] == [{"kind": "auth", "error": "boom"}]
