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
from awf.service.gc_terminal_passes import (
    _dedupe_preserving_order,
    _merge_claude_base_reaps,
)
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


@pytest.mark.unit
async def test_worker_terminal_gc_reaps_base_pinned_only_by_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second pass reaps a claude-base pinned only by a cancelled ws (PRRT_kwDOSJAM6s6JbT1B).

    A superseded ``_shared/claude-base/<sig>`` pinned solely by a cancelled workspace's
    ``base.signature`` stays protected through the first (default-policy) pass — which
    never classifies cancelled rows, so that pin is still on disk when the first pass's
    reaper runs. Only the second discarded-status pass deletes the auth dir (and its
    pin), so the base becomes reapable then: the reaper must run on that pass too, or
    the GB-scale base leaks until a later GC.
    """
    from awf.node.auth_mounts import _shared_claude_base_dir

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
    # The claude-base reaper's own capability probe must also read as capable so an
    # unpinned superseded base is classified as reapable rather than conservatively
    # protected behind the live-mount-unverifiable guard.
    monkeypatch.setattr("awf.service.gc_claude_base._has_cap_sys_admin", lambda: True)

    settings = dataclasses.replace(
        _settings(tmp_path, completed_workspace_retention_hours=24.0),
        claude_base_gc_enabled=True,
        companion_image_cache_enabled=False,
    )
    work_dir = Path(settings.work_dir).resolve()
    old = datetime.now(UTC) - timedelta(hours=200)
    _cancelled_id, cancelled_auth = await _terminal_workspace_with_auth_overlay(
        session_factory,
        work_dir=work_dir,
        status=WorkspaceStatus.cancelled,
        updated_at=old,
    )
    # The cancelled workspace is the sole pin on a superseded shared base; ``host_home``
    # has no ``~/.claude`` so the signature is not the current one and is reapable once
    # unpinned.
    signature = "sigsuperseded000"
    base = _shared_claude_base_dir(work_dir, signature)
    base.mkdir(parents=True)
    (base / "blob").write_text("x" * 512, encoding="utf-8")
    (cancelled_auth / "claude" / "base.signature").write_text(signature, encoding="utf-8")
    assert cancelled_auth.is_dir()
    assert base.parent.is_dir()

    worker_mod.build_worker_runtime(settings)
    reaper = created["terminal_gc_reaper"]

    report = await reaper()

    # The cancelled auth dir (its pin) is reaped, and the now-unpinned base is reclaimed
    # in the same on-demand run — not left until a later GC.
    assert not cancelled_auth.exists()
    assert not base.parent.exists()
    assert report["status"] == "succeeded"
    assert report["claude_base_reap"]["reaped"] == [signature]


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


async def _status_scopes_for_gc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    **reaper_kwargs: Any,
) -> list[tuple[Any, Any]]:
    """Build the reaper, invoke it, and return the (include, exclude) statuses per GC pass."""
    created: dict[str, Any] = {}

    class _ControlWorker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["terminal_gc_reaper"] = kwargs["terminal_gc_reaper"]

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: object())
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    _patch_runtime_constructors(monkeypatch)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)

    scopes: list[tuple[Any, Any]] = []

    async def _fake_gc(_session_factory: object, **kwargs: Any) -> _FakeGCResult:
        scopes.append((kwargs.get("include_statuses"), kwargs.get("exclude_statuses")))
        return _FakeGCResult(1 if len(scopes) == 1 else 0)

    monkeypatch.setattr(worker_mod, "run_service_workspace_gc", _fake_gc)

    settings = dataclasses.replace(
        _settings(tmp_path, completed_workspace_retention_hours=24.0),
        workspace_cleanup_batch_limit=5,
    )
    worker_mod.build_worker_runtime(settings)
    reaper = created["terminal_gc_reaper"]
    await reaper(**reaper_kwargs)
    return scopes


@pytest.mark.unit
async def test_worker_terminal_gc_explicit_status_scope_skips_discarded_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An explicit ``--status`` scope runs one pass with exactly that scope (#590).

    When the operator pins ``--status completed`` the single scoped pass already covers
    exactly the requested terminal statuses, so the cancelled/destroyed augmentation pass
    must not run — otherwise the worker would reclaim cancelled/destroyed auth dirs the
    operator never asked for.
    """
    scopes = await _status_scopes_for_gc(
        monkeypatch,
        tmp_path,
        session_factory,
        statuses=("completed",),
    )

    assert scopes == [(("completed",), None)]


@pytest.mark.unit
async def test_worker_terminal_gc_exclude_status_narrows_both_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``--exclude-status`` removes a status from the default and augmentation passes (#590).

    With no ``--status`` pin the default-policy pass still runs (carrying the operator's
    exclude), and the augmentation pass that reaps cancelled/destroyed must drop the
    excluded ``cancelled`` so the worker never reclaims an auth dir the operator excluded.
    """
    scopes = await _status_scopes_for_gc(
        monkeypatch,
        tmp_path,
        session_factory,
        exclude_statuses=("cancelled",),
    )

    assert scopes == [(None, ("cancelled",)), (("destroyed",), None)]


@pytest.mark.unit
async def test_worker_terminal_gc_excluding_all_discarded_skips_augmentation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Excluding both cancelled and destroyed leaves only the default-policy pass (#590)."""
    scopes = await _status_scopes_for_gc(
        monkeypatch,
        tmp_path,
        session_factory,
        exclude_statuses=("cancelled", "destroyed"),
    )

    assert scopes == [(None, ("cancelled", "destroyed"))]


@pytest.mark.unit
async def test_worker_terminal_gc_no_status_scope_runs_full_two_pass_sweep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No status filters keeps the default two-pass sweep (default policy + cancelled/destroyed)."""
    scopes = await _status_scopes_for_gc(
        monkeypatch,
        tmp_path,
        session_factory,
    )

    assert scopes == [(None, None), (("cancelled", "destroyed"), None)]


@pytest.mark.unit
def test_combine_terminal_gc_reports_concatenates_preserved_payload() -> None:
    """The discarded pass's ``preserved`` entries survive the fold (PRRT_kwDOSJAM6s6JdGCI).

    When the discarded cancelled/destroyed pass only preserves a workspace (still
    inside ``--min-age-hours`` or cleanup disabled) and deletes nothing, the combiner
    must concatenate its ``preserved`` payload — not just sum ``preserved_count`` —
    so the merged report still carries the workspace id and reason that explain why
    the pass reaped nothing.
    """
    default_report: dict[str, Any] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_paths": [],
        "candidates": [],
        "delete_errors": [],
        "preserved": [],
        "preserved_count": 0,
        "total_estimated_bytes": 0,
    }
    discarded_report: dict[str, Any] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_paths": [],
        "candidates": [],
        "delete_errors": [],
        "preserved": [
            {"workspace_id": "ws_cancelled", "reason_code": "WORKSPACE_WITHIN_RETENTION"}
        ],
        "preserved_count": 1,
        "total_estimated_bytes": 0,
    }

    combined = worker_mod._combine_terminal_gc_reports(default_report, discarded_report)

    assert combined["preserved"] == [
        {"workspace_id": "ws_cancelled", "reason_code": "WORKSPACE_WITHIN_RETENTION"}
    ]
    assert combined["preserved_count"] == 1


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
        "total_estimated_bytes": 200,
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
        "total_estimated_bytes": 1_700_000_000,
    }

    combined = worker_mod._combine_terminal_gc_reports(default_report, discarded_report)

    assert combined["status"] == "succeeded"
    assert combined["reason_code"] == "CLEANUP_EXECUTION_SUCCEEDED"
    assert combined["deleted_paths"] == ["/a", "/b", "/c"]
    assert combined["deleted_path_count"] == 3
    assert combined["candidate_count"] == 3
    assert combined["preserved_count"] == 3
    # Both passes reclaim disjoint dirs, so their byte estimates sum rather than the
    # combiner keeping only the default pass's total.
    assert combined["total_estimated_bytes"] == 1_700_000_200


@pytest.mark.unit
def test_combine_terminal_gc_reports_sums_bytes_when_default_pass_empty() -> None:
    """The dominant case #590 flags: a no-candidate default pass + GB-scale discarded pass.

    When the conservative default policy classifies nothing but the discarded
    cancelled/destroyed pass reclaims the multi-GB auth dirs, the merged headline must
    report the bytes the worker actually freed — not the default pass's ``0`` — so the
    operator-facing "report actual GB reclaimed" stays accurate.
    """
    default_report: dict[str, Any] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_paths": [],
        "candidates": [],
        "delete_errors": [],
        "preserved_count": 0,
        "total_estimated_bytes": 0,
    }
    discarded_report: dict[str, Any] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_paths": ["/auth/ws_cancelled"],
        "candidates": [{"workspace_id": "ws_cancelled"}],
        "delete_errors": [],
        "preserved_count": 0,
        "total_estimated_bytes": 1_700_000_000,
    }

    combined = worker_mod._combine_terminal_gc_reports(default_report, discarded_report)

    assert combined["total_estimated_bytes"] == 1_700_000_000


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


@pytest.mark.unit
def test_combine_terminal_gc_reports_merges_per_pass_detail_maps() -> None:
    """A discarded pass partial via a non-delete side effect keeps its detail maps.

    The discarded-status pass can go ``partial`` for a reservation/secret/compose
    teardown failure that never lands in ``delete_errors``. The combiner keeps the
    partial status, so it must also fold the discarded pass's per-workspace detail
    maps in — otherwise operators see ``partial`` next to only the default pass's
    (clean) reservation/secret/teardown maps and cannot tell why the sweep failed.
    """
    from awf.service.gc import CLEANUP_EXECUTION_PARTIAL

    default_report: dict[str, Any] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_paths": [],
        "candidates": [],
        "delete_errors": [],
        "preserved_count": 0,
        "compose_teardowns": {"ws_completed": {"status": "succeeded"}},
        "secret_leases": {"ws_completed": {"status": "revoked"}},
        "worktree_removes": {"ws_completed": {"status": "removed"}},
        "reservation_releases": {"ws_completed": {"error": None}},
    }
    discarded_report: dict[str, Any] = {
        "status": "partial",
        "reason_code": CLEANUP_EXECUTION_PARTIAL,
        "deleted_paths": [],
        "candidates": [],
        "delete_errors": [],
        "preserved_count": 0,
        "compose_teardowns": {"ws_cancelled": {"status": "failed"}},
        "secret_leases": {"ws_cancelled": {"status": "revoke_failed"}},
        "worktree_removes": {"ws_cancelled": {"status": "remove_failed"}},
        "reservation_releases": {"ws_cancelled": {"error": "boom"}},
    }

    combined = worker_mod._combine_terminal_gc_reports(default_report, discarded_report)

    assert combined["status"] == "partial"
    assert combined["reason_code"] == CLEANUP_EXECUTION_PARTIAL
    # Both passes act on disjoint workspaces, so every per-pass detail map is the
    # union — the discarded pass's failing entry is preserved alongside the default's.
    assert combined["reservation_releases"] == {
        "ws_completed": {"error": None},
        "ws_cancelled": {"error": "boom"},
    }
    assert combined["secret_leases"] == {
        "ws_completed": {"status": "revoked"},
        "ws_cancelled": {"status": "revoke_failed"},
    }
    assert combined["compose_teardowns"] == {
        "ws_completed": {"status": "succeeded"},
        "ws_cancelled": {"status": "failed"},
    }
    assert combined["worktree_removes"] == {
        "ws_completed": {"status": "removed"},
        "ws_cancelled": {"status": "remove_failed"},
    }


@pytest.mark.unit
def test_combine_terminal_gc_reports_takes_discarded_detail_map_when_default_absent() -> None:
    """A discarded-only detail map survives when the default pass omitted it."""
    default_report: dict[str, Any] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_paths": [],
        "candidates": [],
        "delete_errors": [],
        "preserved_count": 0,
    }
    discarded_report: dict[str, Any] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_paths": [],
        "candidates": [],
        "delete_errors": [],
        "preserved_count": 0,
        "reservation_releases": {"ws_cancelled": {"error": None}},
    }

    combined = worker_mod._combine_terminal_gc_reports(default_report, discarded_report)

    assert combined["reservation_releases"] == {"ws_cancelled": {"error": None}}


def _reap(status: str, reason_code: str, **lists: Any) -> dict[str, Any]:
    """Build a minimal ``claude_base_reap`` sub-report for merge tests."""
    payload: dict[str, Any] = {
        "status": status,
        "reason_code": reason_code,
        "base_root": "/work/auth/_shared/claude-base",
    }
    payload.update(lists)
    return payload


@pytest.mark.unit
def test_merge_claude_base_reaps_returns_discarded_when_default_absent() -> None:
    """No first-pass reap (e.g. it was skipped): the discarded pass's reap is the whole picture."""
    discarded = _reap("ok", "CLAUDE_BASE_SUPERSEDED_REAPED", reaped=["sigA"])

    merged = _merge_claude_base_reaps(None, discarded)

    assert merged == discarded
    # A fresh dict, so mutating the result never bleeds into the caller's payload.
    assert merged is not discarded


@pytest.mark.unit
def test_merge_claude_base_reaps_returns_none_when_both_absent() -> None:
    """Neither pass produced a reap (base GC disabled): the merged reap is ``None``."""
    assert _merge_claude_base_reaps(None, None) is None


@pytest.mark.unit
def test_merge_claude_base_reaps_returns_default_when_discarded_absent() -> None:
    """The single-pass case (``discarded == {}``) keeps the default pass's reap verbatim."""
    default = _reap("ok", "CLAUDE_BASE_SUPERSEDED_REAPED", reaped=["sigA"])

    merged = _merge_claude_base_reaps(default, None)

    assert merged == default
    assert merged is not default


@pytest.mark.unit
def test_merge_claude_base_reaps_folds_reaped_and_deconflicts_protected() -> None:
    """A base the discarded pass reaped is folded in and dropped from the default ``protected``.

    The default pass kept ``sigA`` protected (its cancelled-workspace pin was still on
    disk); the discarded pass deleted that pin and reaped ``sigA``. The merge surfaces
    the reaped base and removes it from ``protected`` so the summary is not internally
    contradictory, and adopts the discarded pass's ``ok`` status over the default pass's
    no-op ``skipped`` (PRRT_kwDOSJAM6s6JbT1B).
    """
    default = _reap(
        "skipped", "CLAUDE_BASE_GC_NOOP", scanned=["sigA"], protected=["sigA"], reaped=[]
    )
    discarded = _reap(
        "ok", "CLAUDE_BASE_SUPERSEDED_REAPED", scanned=["sigA"], protected=[], reaped=["sigA"]
    )

    merged = _merge_claude_base_reaps(default, discarded)

    assert merged is not None
    assert merged["reaped"] == ["sigA"]
    assert merged["protected"] == []
    assert merged["status"] == "ok"
    assert merged["reason_code"] == "CLAUDE_BASE_SUPERSEDED_REAPED"


@pytest.mark.unit
def test_merge_claude_base_reaps_sums_reaped_estimated_bytes_across_passes() -> None:
    """Each pass reaps disjoint signatures, so their measured byte estimates sum.

    Without folding the byte total, ``dict(default_reap)`` would keep only the first
    pass's bytes and a base the discarded pass reaped would report 0 bytes despite a
    GB-scale removal (PRRT_kwDOSJAM6s6Jcixk).
    """
    default = _reap(
        "ok", "CLAUDE_BASE_SUPERSEDED_REAPED", reaped=["sigA"], reaped_estimated_bytes=1_000
    )
    discarded = _reap(
        "ok",
        "CLAUDE_BASE_SUPERSEDED_REAPED",
        reaped=["sigB"],
        reaped_estimated_bytes=1_700_000_000,
    )

    merged = _merge_claude_base_reaps(default, discarded)

    assert merged is not None
    assert merged["reaped_estimated_bytes"] == 1_700_001_000


@pytest.mark.unit
def test_merge_claude_base_reaps_partial_in_discarded_pass_wins() -> None:
    """A ``partial`` discarded reap drives the merged reap ``partial`` with its reason."""
    default = _reap("ok", "CLAUDE_BASE_SUPERSEDED_REAPED", reaped=["sigA"])
    discarded = _reap(
        "partial",
        "CLAUDE_BASE_REAP_PARTIAL",
        reaped=[],
        errors=[{"signature": "sigB", "reason_code": "CLAUDE_BASE_REAP_PERMISSION_DENIED"}],
    )

    merged = _merge_claude_base_reaps(default, discarded)

    assert merged is not None
    assert merged["status"] == "partial"
    assert merged["reason_code"] == "CLAUDE_BASE_REAP_PARTIAL"
    # The default pass's reclaimed base is still carried alongside the discarded failure.
    assert merged["reaped"] == ["sigA"]
    assert merged["errors"] == [
        {"signature": "sigB", "reason_code": "CLAUDE_BASE_REAP_PERMISSION_DENIED"}
    ]


@pytest.mark.unit
def test_merge_claude_base_reaps_partial_in_default_pass_keeps_default_reason() -> None:
    """A ``partial`` first-pass reap keeps its own reason even when the discarded pass is clean."""
    default = _reap(
        "partial",
        "CLAUDE_BASE_REAP_PARTIAL",
        reaped=[],
        errors=[{"signature": "sigA", "reason_code": "CLAUDE_BASE_REAP_PERMISSION_DENIED"}],
    )
    discarded = _reap("ok", "CLAUDE_BASE_SUPERSEDED_REAPED", reaped=["sigB"])

    merged = _merge_claude_base_reaps(default, discarded)

    assert merged is not None
    assert merged["status"] == "partial"
    assert merged["reason_code"] == "CLAUDE_BASE_REAP_PARTIAL"
    assert merged["reaped"] == ["sigB"]


@pytest.mark.unit
def test_merge_claude_base_reaps_keeps_default_ok_when_both_reaped() -> None:
    """Two clean passes that each reaped a base keep the first pass's ``ok`` and union ``reaped``."""
    default = _reap("ok", "CLAUDE_BASE_SUPERSEDED_REAPED", reaped=["sigA"])
    discarded = _reap("ok", "CLAUDE_BASE_SUPERSEDED_REAPED", reaped=["sigB"])

    merged = _merge_claude_base_reaps(default, discarded)

    assert merged is not None
    assert merged["status"] == "ok"
    assert merged["reaped"] == ["sigA", "sigB"]


@pytest.mark.unit
def test_merge_claude_base_reaps_dedupes_planned_across_passes() -> None:
    """The API dry-run preview can plan the same base in both passes; the merge de-dups it.

    Unlike ``--execute`` (where the first pass removes a base before the second scans it,
    so the lists are disjoint), the dry-run preview deletes nothing and threads the first
    pass's planned auth dirs into the second pass — so a base pinned only by a default
    candidate is planned by both passes. The merged ``planned`` must list it once
    (PRRT_kwDOSJAM6s6Jbinh), and a base pinned by *both* a default and a discarded
    candidate (kept ``protected`` in the first pass) folds in as ``planned``, not
    ``protected``.
    """
    default = _reap(
        "ok",
        "CLAUDE_BASE_SUPERSEDED_PLANNED",
        scanned=["sigDefaultOnly", "sigBoth"],
        planned=["sigDefaultOnly"],
        protected=["sigBoth"],
    )
    discarded = _reap(
        "ok",
        "CLAUDE_BASE_SUPERSEDED_PLANNED",
        scanned=["sigDefaultOnly", "sigBoth"],
        planned=["sigDefaultOnly", "sigBoth"],
        protected=[],
    )

    merged = _merge_claude_base_reaps(default, discarded)

    assert merged is not None
    assert merged["planned"] == ["sigDefaultOnly", "sigBoth"]
    assert merged["protected"] == []


def test_merge_claude_base_reaps_tolerates_unhashable_reaped_planned_entries() -> None:
    """A malformed unhashable entry in ``reaped``/``planned`` must not crash the merge.

    ``_dedupe_preserving_order`` keeps unhashable entries (a dict/list from a malformed
    worker JSON payload) without raising, but the ``protected`` de-confliction must not
    re-introduce the crash by expanding those lists into a set (PRRT_kwDOSJAM6s6JcnXn).
    """
    default = _reap(
        "ok",
        "CLAUDE_BASE_SUPERSEDED_REAPED",
        reaped=[{"malformed": "reaped"}, "sigA"],
        protected=["sigA", "sigProtected"],
    )
    discarded = _reap(
        "ok",
        "CLAUDE_BASE_SUPERSEDED_PLANNED",
        planned=[["malformed", "planned"], "sigB"],
        protected=["sigB"],
    )

    merged = _merge_claude_base_reaps(default, discarded)

    assert merged is not None
    assert merged["reaped"] == [{"malformed": "reaped"}, "sigA"]
    assert merged["planned"] == [["malformed", "planned"], "sigB"]
    # Hashable signatures still reaped/planned by either pass drop out of ``protected``;
    # the unrelated ``sigProtected`` survives.
    assert merged["protected"] == ["sigProtected"]


@pytest.mark.unit
def test_dedupe_preserving_order_keeps_unhashable_entries_without_raising() -> None:
    """Unhashable entries fall through the ``except TypeError`` arm and are kept as-is.

    The merge helpers de-dup ``<signature>`` strings, but a malformed worker JSON
    payload could carry unhashable entries (a dict or list) in those positions.
    ``value in seen`` raises ``TypeError`` for those, and the documented defensive
    behavior keeps each one (no exception), while still collapsing hashable
    duplicates in first-seen order.
    """
    result = _dedupe_preserving_order([{"a": 1}, "x", {"a": 1}, "x"])

    # The hashable duplicate ("x") is collapsed to a single first-seen entry, while
    # both unhashable dicts are preserved (kept as-is, not deduped) in order.
    assert result == [{"a": 1}, "x", {"a": 1}]
