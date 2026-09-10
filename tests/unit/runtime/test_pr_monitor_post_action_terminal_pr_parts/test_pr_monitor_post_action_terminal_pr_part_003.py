"""#910 post-action terminal-PR guard — part 3 of 3.

Terminal-cycle ownership: the terminate sinks fenced against a superseded monitor
owner, the state a moot cycle may and may not persist, terminal-artifact
publication ordering against cancellable cleanup, and the
``ComposeExecCleanupError`` seam that must recheck live PR state before failing.

Part 1 holds the end-to-end regression and the push/pause seams; part 2 holds the
notification and fail-open re-check paths. Shared builders live in ``._helpers``;
the PostgreSQL ``factory`` fixture lives in the package ``conftest``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED, ComposeExecCleanupError
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.runtime.pr_monitor import (
    Abort,
    AbortReason,
    AddressComments,
    AddressOperatorHint,
    CheckFailure,
    MonitorState,
    OperatorHint,
    ReportCiFailure,
    ReviewThread,
    ShortCircuitCompleted,
    SyncBase,
)
from awf.runtime.pr_monitor_runner.constants import (
    _GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON,
    _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
)
from awf.runtime.pr_monitor_runner.loop_helpers import (
    _finish_cycle_for_terminal_pr,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import (
    _PostActionPrTerminalState,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_post_action_terminal_pr_parts._helpers import (
    _moot_events,
    _respond_to_git_probes,
    _ScriptedGh,
    _status,
)


@pytest.mark.unit
async def test_terminate_completed_is_fenced_against_a_superseded_monitor_owner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A superseded runner must not complete the live claimant's workspace.

    ``_finish_cycle_for_terminal_pr`` routes a merged observation straight into
    ``_terminate_completed``. A long action can lose its monitor claim mid-flight
    (``claim_monitoring_pr`` reassigns expired leases) and only then observe the
    merge, so the merged sink needs the same owner fence as ``_terminate_failed``:
    the status guard alone misses the race because the takeover leaves the row in
    ``monitoring_pr``.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-stale"  # lease lost to worker-current

    await runner._terminate_completed(
        workspace_id,
        pr_merge_sha="mergesha0000",
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        ignored = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.stale_callback_ignored",
            limit=10,
        )
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert workspace.pr_merge_sha is None
    assert len(ignored) == 1
    assert ignored[0].payload["callback_action"] == "terminal_completed"


@pytest.mark.unit
async def test_terminate_completed_publishes_before_cancellable_session_exit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Cancellation in session exit cannot strand the terminal publication.

    Regression for PRRT_kwDOSJAM6s6g8fr. Once the ``completed`` transition
    commits, no later monitor retries the defer signal. The commit-adjacent
    callback must therefore finish before the session context enters its
    cancellable ``__aexit__``.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        await session.commit()

    session_exit_started = asyncio.Event()

    @asynccontextmanager
    async def _cancellable_exit_factory() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
            finally:
                session_exit_started.set()
                await asyncio.Event().wait()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_ScriptedGh(),
    )
    runner._deps = replace(runner._deps, session_factory=_cancellable_exit_factory)
    runner._monitor_owner_id = "worker-current"
    callback_finished = asyncio.Event()

    async def _publish() -> None:
        callback_finished.set()

    completion_task = asyncio.create_task(
        runner._terminate_completed(
            workspace_id,
            pr_merge_sha=None,
            on_transition_committed=_publish,
        )
    )
    await session_exit_started.wait()
    completion_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await completion_task

    assert callback_finished.is_set()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "completed"


@pytest.mark.unit
async def test_terminate_completed_shields_post_commit_publication_from_cancellation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Outer cancellation waits for terminal publication before propagating.

    Regression for PRRT_kwDOSJAM6s6g8fr. The callback can grow await points even
    though today's defer-signal writer is synchronous; cancellation at any such
    point must not cancel the publication owned by the committed transition.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-current"
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    callback_finished = asyncio.Event()

    async def _publish() -> None:
        callback_started.set()
        await release_callback.wait()
        callback_finished.set()

    completion_task = asyncio.create_task(
        runner._terminate_completed(
            workspace_id,
            pr_merge_sha=None,
            on_transition_committed=_publish,
        )
    )
    await callback_started.wait()
    completion_task.cancel()
    await asyncio.sleep(0)

    assert not completion_task.done()
    completion_task.cancel()
    await asyncio.sleep(0)
    assert not completion_task.done()
    release_callback.set()
    with pytest.raises(asyncio.CancelledError):
        await completion_task

    assert callback_finished.is_set()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "completed"


def _merged_terminal_push_result(*, merge_commit_sha: str = "mergesha0000") -> _GitPushResult:
    """A moot push envelope carrying a post-action ``merged`` observation."""
    return _GitPushResult(
        pushed=False,
        failed=False,
        returncode=0,
        reason_code=_MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
        pr_terminal=_PostActionPrTerminalState(
            status=_status(merged=True, merge_commit_sha=merge_commit_sha),
            local_head_sha="localhead1234",
        ),
    )


def _stale_state() -> MonitorState:
    """In-memory monitor state a superseded runner must not flush."""
    state = MonitorState()
    state.mark_addressed("t-stale", "fix_committed")
    state.last_push_sha = "stalesha0000"
    return state


@pytest.mark.unit
async def test_terminal_moot_cycle_does_not_persist_state_for_a_superseded_owner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A superseded runner must not flush monitor state or the defer signal.

    ``_persist_state`` and ``_write_defer_signal`` are not fenced on
    ``monitor_claimed_by``, so running them ahead of the ``_terminate_completed``
    owner fence let a claim-losing runner overwrite the live claimant's
    ``monitor_threads_addressed`` / ``monitor_last_commit_sha`` and publish a
    "monitor is done" drop while the row stayed ``monitoring_pr``
    (PRRT_kwDOSJAM6s6flswY).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        workspace.monitor_threads_addressed = {"t-live": "fix_committed"}
        workspace.monitor_last_commit_sha = "livesha00000"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-stale"  # lease lost to worker-current
    state = _stale_state()

    moot = await _finish_cycle_for_terminal_pr(
        runner,
        workspace_id=workspace_id,
        operation=None,
        push_result=_merged_terminal_push_result(),
        state=state,
        pr_number=42,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    # The action is still moot for THIS runner: it must end its cycle either way.
    assert moot is True
    # ...but the refusal is propagated so the outer loop drops its own persist
    # instead of flushing this superseded state (PRRT_kwDOSJAM6s6fsqcA).
    assert state.monitor_writes_suppressed is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert workspace.monitor_threads_addressed == {"t-live": "fix_committed"}
    assert workspace.monitor_last_commit_sha == "livesha00000"
    assert not (artifacts_root / f"{workspace_id}.defer-signal.json").exists()


@pytest.mark.unit
async def test_terminal_moot_cycle_persists_state_for_the_owning_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The still-owning runner keeps flushing its state and the defer signal."""
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-current"
    state = _stale_state()

    moot = await _finish_cycle_for_terminal_pr(
        runner,
        workspace_id=workspace_id,
        operation=None,
        push_result=_merged_terminal_push_result(),
        state=state,
        pr_number=42,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert moot is True
    # The write committed, so the outer loop's persist stays enabled.
    assert state.monitor_writes_suppressed is False
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "completed"
    assert workspace.pr_merge_sha == "mergesha0000"
    assert workspace.monitor_threads_addressed is not None
    assert workspace.monitor_threads_addressed["t-stale"] == "fix_committed"
    assert workspace.monitor_last_commit_sha == "stalesha0000"
    signal = json.loads((artifacts_root / f"{workspace_id}.defer-signal.json").read_text())
    assert signal["terminal_action"] == "ShortCircuitCompleted"
    assert signal["merged"] is True


@pytest.mark.unit
async def test_run_does_not_flush_superseded_state_after_a_terminal_moot_cycle(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """``run()`` must honor the terminal cycle's refusal to write.

    Regression for PRRT_kwDOSJAM6s6fsqcA. Every arm returns ``True`` after
    ``_finish_cycle_for_terminal_pr``, which lands on ``run()``'s unconditional
    post-``_execute`` ``_persist_state``. Without the propagated suppression that
    outer persist re-introduced exactly what the owner fence had just dropped:
    the superseded runner's ``monitor_threads_addressed`` / ``monitor_last_commit_sha``
    overwriting the live claimant's row, so a still-open thread could read as
    addressed and auto-merge could bypass live feedback.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        workspace.monitor_threads_addressed = {"t-live": "fix_committed"}
        workspace.monitor_last_commit_sha = "livesha00000"
        await session.commit()
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    artifacts_root = tmp_path / "artifacts"
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(_status()),  # decide() sees the PR still open
    )

    async def _execute_into_terminal_pr(*, state: MonitorState, **_kwargs: object) -> bool:
        """Mirror an arm whose long action outlived its PR, claim already lost."""
        state.mark_addressed("t-stale", "fix_committed")
        state.last_push_sha = "stalesha0000"
        return await _finish_cycle_for_terminal_pr(
            runner,
            workspace_id=workspace_id,
            operation=None,
            push_result=_merged_terminal_push_result(),
            state=state,
            pr_number=42,
            repo_url="git@github.com:dimileeh/aira-web.git",
            base_branch="development",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    runner._execute = _execute_into_terminal_pr  # type: ignore[method-assign]

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_owner_id="worker-stale",  # lease lost to worker-current
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    threads_addressed = workspace.monitor_threads_addressed or {}
    assert "t-stale" not in threads_addressed
    assert threads_addressed["t-live"] == "fix_committed"
    assert workspace.monitor_last_commit_sha == "livesha00000"
    assert not (artifacts_root / f"{workspace_id}.defer-signal.json").exists()


@pytest.mark.unit
async def test_persist_state_refuses_to_write_a_superseded_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The suppression fence lives at the write seam, not only at one call site.

    Regression for PRRT_kwDOSJAM6s6fsqcA. ``_finish_cycle_for_terminal_pr`` marks
    the state superseded when the terminate sink refuses behind the owner fence,
    but honoring that only in ``run()``'s post-``_execute`` persist leaves every
    other ``_persist_state`` caller (the pre-``_execute`` flush, the provider
    recovery handlers) free to write the same stale snapshot onto the live
    claimant's row. Fence the write itself so the refusal cannot be routed around.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        workspace.monitor_threads_addressed = {"t-live": "fix_committed"}
        workspace.monitor_last_commit_sha = "livesha00000"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_ScriptedGh(),
    )
    state = _stale_state()
    state.monitor_writes_suppressed = True

    await runner._persist_state(workspace_id, state)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.monitor_threads_addressed == {"t-live": "fix_committed"}
    assert workspace.monitor_last_commit_sha == "livesha00000"


def _cancelled_post_merge_reconciler(
    artifacts_root: Path, observed: dict[str, object]
) -> Callable[..., Any]:
    """Reconciler that records artifact presence, then cancels the cleanup.

    ``_terminate_completed`` runs the target-branch reconcile AFTER committing the
    ``completed`` transition, so this double stands in for a cancellation (worker
    shutdown, process loss) landing anywhere in that post-commit cleanup.
    """

    async def _reconcile(*, repo_url: str, branch: str, workspace_id: str) -> None:
        del repo_url, branch
        observed["artifact_present"] = (
            artifacts_root / f"{workspace_id}.defer-signal.json"
        ).exists()
        raise asyncio.CancelledError

    return _reconcile


@pytest.mark.unit
async def test_terminal_moot_cycle_publishes_artifacts_before_cancellable_cleanup(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The gated writes land at the transition commit, not after the cleanup.

    Regression for PRRT_kwDOSJAM6s6fvDbP. ``_terminate_completed`` commits the
    ``completed`` transition and only then reconciles the target branch and GCs the
    workspace filesystem. Waiting for it to RETURN before publishing meant a
    cancellation inside that post-commit cleanup left a terminal row whose defer
    artifact no later monitor would ever write — the row is no longer
    ``monitoring_pr``, so nothing re-runs the monitor for it.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    observed: dict[str, object] = {}
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
        post_merge_target_reconciler=_cancelled_post_merge_reconciler(artifacts_root, observed),
    )
    runner._monitor_owner_id = "worker-current"
    state = _stale_state()

    with pytest.raises(asyncio.CancelledError):
        await _finish_cycle_for_terminal_pr(
            runner,
            workspace_id=workspace_id,
            operation=None,
            push_result=_merged_terminal_push_result(),
            state=state,
            pr_number=42,
            repo_url="git@github.com:dimileeh/aira-web.git",
            base_branch="development",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    # Both gated writes ran before the cancellable cleanup was even entered.
    assert observed["artifact_present"] is True
    signal = json.loads((artifacts_root / f"{workspace_id}.defer-signal.json").read_text())
    assert signal["terminal_action"] == "ShortCircuitCompleted"
    assert signal["merged"] is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "completed"
    assert workspace.monitor_last_commit_sha == "stalesha0000"


@pytest.mark.unit
async def test_short_circuit_arm_publishes_defer_signal_before_cancellable_cleanup(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Same commit-adjacent publication for the ``ShortCircuitCompleted`` arm.

    Regression for PRRT_kwDOSJAM6s6fvDbP: the arm gates the defer signal on the
    terminate sink's owner fence, but must not delay it until the sink's cancellable
    post-commit reconcile + filesystem GC have finished.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    observed: dict[str, object] = {}
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
        post_merge_target_reconciler=_cancelled_post_merge_reconciler(artifacts_root, observed),
    )
    runner._monitor_owner_id = "worker-current"
    state = _stale_state()

    with pytest.raises(asyncio.CancelledError):
        await runner._execute(
            action=ShortCircuitCompleted(),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_status(merged=True, merge_commit_sha="mergesha0000"),
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    assert observed["artifact_present"] is True
    signal = json.loads((artifacts_root / f"{workspace_id}.defer-signal.json").read_text())
    assert signal["terminal_action"] == "ShortCircuitCompleted"
    assert signal["merged"] is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "completed"


@pytest.mark.unit
async def test_short_circuit_arm_skips_defer_signal_for_a_superseded_owner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The ``ShortCircuitCompleted`` arm fences its writes on the terminate sink.

    Regression for PRRT_kwDOSJAM6s6fsrlC. The arm used to publish the defer signal
    BEFORE ``_terminate_completed``, so a runner that lost its monitor claim
    mid-cycle left a workspace-scoped "monitor is done" artifact (and, via
    ``run()``'s post-``_execute`` persist, its stale monitor state) while the live
    claimant's row stayed ``monitoring_pr``.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        workspace.monitor_last_commit_sha = "livesha00000"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-stale"  # lease lost to worker-current
    state = _stale_state()

    terminal = await runner._execute(
        action=ShortCircuitCompleted(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(merged=True, merge_commit_sha="mergesha0000"),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    # The cycle still ends for THIS runner; only its workspace writes are dropped.
    assert terminal is True
    assert state.monitor_writes_suppressed is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert workspace.monitor_last_commit_sha == "livesha00000"
    assert not (artifacts_root / f"{workspace_id}.defer-signal.json").exists()


@pytest.mark.unit
async def test_moot_arm_returns_the_terminate_sinks_refusal_not_an_unconditional_true(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A moot arm reports the terminate sink's result, not a blanket ``True``.

    Regression for PRRT_kwDOSJAM6s6fsqcA at the anchored seam. Every arm used to
    ``return True`` immediately after ``_finish_if_pr_terminal``, so a cycle whose
    terminate sink had refused the write — this runner superseded as the monitor
    owner — still announced itself to ``run()`` as a completed terminal cycle,
    which is exactly the signal that lands on the post-``_execute``
    ``_persist_state``. The arm now returns the sink's own verdict, so a
    superseded cycle can never be mistaken for one that terminated the workspace.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        workspace.monitor_last_commit_sha = "livesha00000"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-stale"  # lease lost to worker-current
    state = _stale_state()

    async def _moot_sync_base(**_kwargs: object) -> _GitPushResult:
        """The sync-base push that only then observed the merged PR."""
        return _merged_terminal_push_result()

    runner._run_sync_base = _moot_sync_base  # type: ignore[method-assign]

    terminal = await runner._execute(
        action=SyncBase(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.monitor_writes_suppressed is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert workspace.monitor_last_commit_sha == "livesha00000"
    assert not (artifacts_root / f"{workspace_id}.defer-signal.json").exists()


@pytest.mark.unit
async def test_terminal_moot_cycle_skips_defer_signal_for_a_superseded_abort(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The closed-PR arm fences on ``_terminate_failed`` the same way."""
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        workspace.monitor_last_commit_sha = "livesha00000"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-stale"

    moot = await _finish_cycle_for_terminal_pr(
        runner,
        workspace_id=workspace_id,
        operation=None,
        push_result=_GitPushResult(
            pushed=False,
            failed=False,
            returncode=0,
            reason_code=_MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
            pr_terminal=_PostActionPrTerminalState(status=_status(closed=True)),
        ),
        state=_stale_state(),
        pr_number=42,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert moot is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert workspace.monitor_last_commit_sha == "livesha00000"
    assert not (artifacts_root / f"{workspace_id}.defer-signal.json").exists()


@pytest.mark.unit
async def test_abort_arm_skips_defer_signal_for_a_superseded_owner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The ``Abort`` arm fences its writes on the terminate sink too.

    Same defect as the ``ShortCircuitCompleted`` arm (PRRT_kwDOSJAM6s6fsrlC): the
    sibling terminal arm published the defer signal BEFORE ``_terminate_failed``,
    so a runner that lost its monitor claim mid-cycle left a workspace-scoped
    "monitor is done" artifact (and, via ``run()``'s post-``_execute`` persist, its
    stale monitor state) while the live claimant's row stayed ``monitoring_pr``.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        workspace.monitor_last_commit_sha = "livesha00000"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-stale"  # lease lost to worker-current
    state = _stale_state()

    terminal = await runner._execute(
        action=Abort(reason=AbortReason.pr_closed_externally),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(closed=True),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    # The cycle still ends for THIS runner; only its workspace writes are dropped.
    assert terminal is True
    assert state.monitor_writes_suppressed is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert workspace.monitor_last_commit_sha == "livesha00000"
    assert not (artifacts_root / f"{workspace_id}.defer-signal.json").exists()


# ---------------------------------------------------------------------------
# The compose-exec cleanup handlers (PRRT_kwDOSJAM6s6fvDbL).
# ---------------------------------------------------------------------------


def _cleanup_error(label: str) -> ComposeExecCleanupError:
    """The cleanup fault a long agent action raises out of its ``_run_*``."""
    return ComposeExecCleanupError(
        invocation_id="awf-monitor-cleanup",
        source="agent",
        label=label,
        message="tagged process still alive",
    )


_CLEANUP_HINT = OperatorHint(
    reason="repair after operator guide",
    directive="fix it",
    operation_id="op_operator_hint_cleanup",
    requested_at="2026-06-27T00:00:00+00:00",
    reason_code="OPERATOR_GUIDE",
)
_CLEANUP_THREAD = ReviewThread(
    thread_id="T_open",
    path="src/foo.py",
    line=12,
    body_excerpt="please fix",
    author="reviewer",
)
_CLEANUP_ARMS = [
    pytest.param("_run_sync_base", SyncBase(), "sync_base", id="sync_base"),
    pytest.param(
        "_run_ci_fix",
        ReportCiFailure(
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="boom"),)
        ),
        "ci_repair",
        id="ci_repair",
    ),
    pytest.param(
        "_run_fix_cycle",
        AddressComments(threads=(_CLEANUP_THREAD,), review_comments=()),
        "comment_repair",
        id="comment_repair",
    ),
    pytest.param(
        "_run_operator_hint_cycle",
        AddressOperatorHint(hint=_CLEANUP_HINT),
        "comment_repair",
        id="operator_hint",
    ),
]


def _cleanup_state(action: object) -> MonitorState:
    """Monitor state for a cleanup-arm ``_execute`` call."""
    if isinstance(action, AddressOperatorHint):
        return MonitorState(started_at=0.0, pending_operator_hint=_CLEANUP_HINT)
    return MonitorState(started_at=0.0)


@pytest.mark.unit
@pytest.mark.parametrize(("run_method", "action", "operation_type"), _CLEANUP_ARMS)
async def test_cleanup_failure_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_method: str,
    action: object,
    operation_type: str,
) -> None:
    """A cleanup fault on a merged PR must complete the workspace, not fail it.

    ``ComposeExecCleanupError`` escapes the ``_run_*`` helpers as an exception, so
    it bypasses both their own post-action guards and the arm's
    ``_finish_if_pr_terminal`` call below the ``try``. The handler recorded
    ``EXEC_PROCESS_CLEANUP_FAILED`` and terminally failed a workspace whose PR had
    merged while the long action ran (PRRT_kwDOSJAM6s6fvDbL).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="unpushed-repair-head")
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise_cleanup_error(**_kwargs: object) -> object:
        raise _cleanup_error(operation_type)

    monkeypatch.setattr(runner, run_method, _raise_cleanup_error)

    terminal = await runner._execute(
        action=action,
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(),  # the snapshot decide() ran on: PR still open
        state=_cleanup_state(action),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    events = await _moot_events(factory, workspace_id)
    assert len(events) == 1
    payload = events[0].payload  # type: ignore[attr-defined]
    assert payload["pr_state"] == "merged"
    assert payload["local_head_sha"] == "unpushed-repair-head"
    assert payload["operation_type"] == operation_type
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert workspace is not None
    assert workspace.status == "completed"
    assert workspace.pr_merge_sha == "mergesha0000"
    operation = operations[0]
    assert operation.status == OperationStatus.succeeded.value
    assert operation.result is not None
    assert operation.result["outcome"] == "pr_terminal_moot"


@pytest.mark.unit
async def test_cleanup_failure_still_terminates_when_pr_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recheck must not soften a cleanup fault while the PR is still open.

    A stranded agent process on a live PR stays a terminal
    ``EXEC_PROCESS_CLEANUP_FAILED`` failure — the guard only changes the outcome
    when the PR itself already ended.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status())  # cleanup-path recheck: PR still open
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise_cleanup_error(**_kwargs: object) -> object:
        raise _cleanup_error("ci_repair")

    monkeypatch.setattr(runner, "_run_ci_fix", _raise_cleanup_error)

    terminal = await runner._execute(
        action=ReportCiFailure(
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="boom"),)
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert len(await _moot_events(factory, workspace_id)) == 0
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert workspace is not None
    assert workspace.status == "failed"
    assert "EXEC_PROCESS_CLEANUP_FAILED" in (workspace.failure_message or "")
    operation = operations[0]
    assert operation.status == OperationStatus.failed.value
    assert operation.error_code == EXEC_PROCESS_CLEANUP_FAILED


# ---------------------------------------------------------------------------
# The workflow-scope escalation propagates its notification-boundary re-read
# (PRRT_kwDOSJAM6s6fvGsp).
# ---------------------------------------------------------------------------


_WORKFLOW_SCOPE_ARMS = [
    pytest.param("_run_sync_base", SyncBase(), "sync_base", id="sync_base"),
    pytest.param(
        "_run_ci_fix",
        ReportCiFailure(
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="boom"),)
        ),
        "ci_repair",
        id="ci_repair",
    ),
]


def _workflow_scope_rejection() -> _GitPushResult:
    """The push GitHub rejected for a missing ``workflow`` token scope."""
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="refusing to allow an OAuth App to create or update workflow",
        reason_code=_GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON,
    )


async def _drive_workflow_scope_arm(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_method: str,
    action: object,
    recheck: object,
) -> tuple[str, _ScriptedGh, bool]:
    """Run a workflow-scope-failing arm with a scripted notification re-read."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="unpushed-workflow-head")
    gh = _ScriptedGh(recheck)  # type: ignore[arg-type]
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _reject_for_workflow_scope(**_kwargs: object) -> _GitPushResult:
        return _workflow_scope_rejection()

    monkeypatch.setattr(runner, run_method, _reject_for_workflow_scope)

    terminal = await runner._execute(
        action=action,
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(),  # the snapshot decide() ran on: PR still open
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )
    return workspace_id, gh, bool(terminal)


@pytest.mark.unit
@pytest.mark.parametrize(("run_method", "action", "operation_type"), _WORKFLOW_SCOPE_ARMS)
async def test_workflow_scope_failure_is_moot_for_a_pr_that_merged_mid_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_method: str,
    action: object,
    operation_type: str,
) -> None:
    """A PR that merged during the push must complete, not terminally fail.

    The last #910 recheck sits BEFORE the push, so only the notification
    boundary's fresh read sees a PR that merged between the two. That observation
    used to be discarded, and these arms went on to ``_terminate_failed`` a
    workspace whose PR had merged (PRRT_kwDOSJAM6s6fvGsp).
    """
    workspace_id, gh, terminal = await _drive_workflow_scope_arm(
        factory,
        tmp_path,
        monkeypatch,
        run_method=run_method,
        action=action,
        recheck=_status(merged=True, merge_commit_sha="mergesha0000"),
    )

    assert terminal is True
    assert gh.posts == []  # no stale needs-human ping on a merged PR
    events = await _moot_events(factory, workspace_id)
    assert len(events) == 1
    payload = events[0].payload  # type: ignore[attr-defined]
    assert payload["context"] == "workflow_scope_notification"
    assert payload["local_head_sha"] == "unpushed-workflow-head"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert workspace is not None
    assert workspace.status == "completed"
    assert workspace.pr_merge_sha == "mergesha0000"
    # The arm's own operation keeps the genuine push failure it already recorded.
    operation = operations[0]
    assert operation.status == OperationStatus.failed.value
    assert operation.error_code == _GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON
    assert operation.result is not None
    assert operation.result["reason_code"] == _GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON
    assert operation.type == operation_type


@pytest.mark.unit
@pytest.mark.parametrize(("run_method", "action", "operation_type"), _WORKFLOW_SCOPE_ARMS)
async def test_workflow_scope_failure_still_fails_when_pr_stayed_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_method: str,
    action: object,
    operation_type: str,
) -> None:
    """Guard-does-not-regress: a live PR still gets the ping and the terminal fail."""
    del operation_type
    workspace_id, gh, terminal = await _drive_workflow_scope_arm(
        factory,
        tmp_path,
        monkeypatch,
        run_method=run_method,
        action=action,
        recheck=_status(),
    )

    assert terminal is True
    assert len(gh.posts) == 1
    assert await _moot_events(factory, workspace_id) == []
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "failed"
    assert "workflow" in (workspace.failure_message or "")


@pytest.mark.unit
@pytest.mark.parametrize(("run_method", "action", "operation_type"), _WORKFLOW_SCOPE_ARMS)
async def test_workflow_scope_failure_aborts_for_a_pr_that_closed_mid_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_method: str,
    action: object,
    operation_type: str,
) -> None:
    """A PR closed mid-push fails as ``pr_closed_externally``, not as the push error.

    The propagated observation carries the CLOSED half of the terminal handling
    too: the workspace still ends ``failed``, but through the abort ``decide()``
    would return next poll, so the operator sees "the PR was closed" rather than a
    token-scope blocker that no longer matters (PRRT_kwDOSJAM6s6fvGsp).
    """
    del operation_type
    workspace_id, gh, terminal = await _drive_workflow_scope_arm(
        factory,
        tmp_path,
        monkeypatch,
        run_method=run_method,
        action=action,
        recheck=_status(closed=True),
    )

    assert terminal is True
    assert gh.posts == []  # no stale needs-human ping on a closed PR
    events = await _moot_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0].payload["pr_state"] == "closed"  # type: ignore[attr-defined]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "failed"
    assert AbortReason.pr_closed_externally.value in (workspace.failure_message or "")
    assert "workflow" not in (workspace.failure_message or "")
