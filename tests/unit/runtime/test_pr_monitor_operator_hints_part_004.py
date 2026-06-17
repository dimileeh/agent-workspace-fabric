"""Regression tests for operator remonitor hints — preserved-commit no-op slice (part 4).

Split out of ``test_pr_monitor_operator_hints_part_002`` to keep that module under
the first-party line limit. These cases cover ``_preserved_commit_already_on_remote``
(the idempotent no-op-push guard that refuses to drop an approved protected commit when
a worktree was reset to the remote head) and the atomic persistence of the preserved
HEAD marker on the ``monitoring_pr -> blocked`` transition.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.quality_gates import QualityGateViolation
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    MonitorState,
)
from awf.runtime.pr_monitor_runner.remote_ops import _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changed_paths", "expected"),
    [((), True), (("src/awf/x.py",), False)],
)
async def test_preserved_commit_already_on_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_paths: tuple[str, ...],
    expected: bool,
) -> None:
    """An empty changed-path set vs the remote PR branch means the preserved
    commit is already pushed (no-op); a non-empty set means there is work to push."""
    worktree = tmp_path / "worktrees" / "ws_div"
    worktree.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("base-sha", changed_paths)

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    result = await runner._preserved_commit_already_on_remote(
        workspace_id="ws_div",
        worktree_path=worktree,
        remote_branch="awf/ws_div",
    )
    assert result is expected


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_missing_worktree_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_missing",
            worktree_path=tmp_path / "worktrees" / "ws_missing",
            remote_branch="awf/ws_missing",
        )
        is False
    )


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_diff_error_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktrees" / "ws_err"
    worktree.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        raise ProtectedScopeDiffError("fetch failed")

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_err",
            worktree_path=worktree,
            remote_branch="awf/ws_err",
        )
        is False
    )


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_recorded_sha_not_on_remote_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worktree reset/recreated at the remote head during the blocked interval:
    the diff is empty but the recorded preserved SHA is NOT on the branch, so the
    no-op MUST be refused or the approved protected commit is silently dropped."""
    worktree = tmp_path / "worktrees" / "ws_reset"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    # merge-base --is-ancestor <preserved> FETCH_HEAD exits non-zero: not on remote.
    cmd.queue_result(returncode=1)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("base-sha", ())

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_reset",
            worktree_path=worktree,
            remote_branch="awf/ws_reset",
            preserved_head_sha="preserved-sha",
        )
        is False
    )
    ancestry_calls = [c for c in cmd.calls if "--is-ancestor" in c.args]
    assert ancestry_calls, "expected a merge-base --is-ancestor verification"
    assert "preserved-sha" in ancestry_calls[0].args
    assert "FETCH_HEAD" in ancestry_calls[0].args


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_recorded_sha_on_remote_returns_true(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine idempotent restart: the preserved commit truly landed on the
    remote (ancestry check passes), so the push is a legitimate no-op."""
    worktree = tmp_path / "worktrees" / "ws_idem"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("base-sha", ())

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_idem",
            worktree_path=worktree,
            remote_branch="awf/ws_idem",
            preserved_head_sha="preserved-sha",
        )
        is True
    )


@pytest.mark.unit
async def test_protected_block_persists_preserved_head_marker_atomically(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preserved HEAD SHA must be durable on the workspace row AS SOON AS the
    ``monitoring_pr -> blocked`` transition commits — not only in the in-memory
    ``state`` that the loop flushes later. A crash after the block commit but
    before ``_persist_state`` would otherwise lose the only monitor-state copy of
    the preserved head; the next grant-only resume would read
    ``preserved_head_sha=None`` and treat a reset/recreated worktree's empty diff
    as already-pushed, silently dropping the approved commit (PRRT_kwDOSJAM6s6KEtU6)."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "blocked-head-sha"

    async def _no_notification(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_rev_parse_head", _head)
    monkeypatch.setattr(runner, "_post_protected_block_notification", _no_notification)

    # A fresh state whose in-memory marker we deliberately IGNORE afterwards: the
    # durable copy must come from the block commit, not from a later flush.
    state = MonitorState()
    block = _ProtectedScopePushBlock(
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            ),
        ),
    )

    result = await runner._pause_monitor_for_protected_scope_block(
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="start-sha",
        protected_scope_block=block,
        worktree_path=worktree,
        state=state,
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.paused_into_blocked is True
    # The marker is durable on the row WITHOUT any later ``_persist_state`` flush.
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == "blocked"
        assert (workspace.monitor_threads_addressed or {}).get(
            _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY
        ) == "blocked-head-sha"
