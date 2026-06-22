"""HUMAN_WAIT attention-flag SET/CLEAR behavior in the PR-monitor runner (#657).

The monitor surfaces a ``NotifyHuman`` escalation as a persisted, operator-visible
attention flag on the still-``monitoring_pr`` row (not a new ``WorkspaceStatus``).
These tests pin the two centralized touch-points in ``loop._execute``:

- SET — a ``NotifyHuman`` action stamps ``awaiting_human_since`` (stable across
  repeats) and refreshes ``awaiting_human_reason``.
- CLEAR — any non-``NotifyHuman`` action (the monitor resumed) and terminal exits
  null both columns.

``NotifyHuman(message=...)`` short-circuits ``_is_manual_ready_handoff`` to False,
so every call falls straight through to the human_wait touch-point — deterministic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ShortCircuitCompleted,
    WaitForCI,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_HEAD_SHA = "abc1234567890def"
_PR_NUMBER = 42


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a PostgreSQL-backed async session factory for monitor tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _green_status() -> PRStatus:
    return PRStatus(
        number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


def _runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> object:
    return make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )


async def _execute(
    runner: object,
    *,
    action: object,
    workspace_id: str,
    tmp_path: Path,
    state: MonitorState,
) -> bool:
    return await runner._execute(  # type: ignore[attr-defined]
        action=action,
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=_PR_NUMBER,
        status=_green_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )


async def _read_attention(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> tuple[object, str | None, str]:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        return ws.awaiting_human_since, ws.awaiting_human_reason, ws.status


@pytest.mark.unit
async def test_notify_human_sets_attention_flag(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(
        factory, pr_number=_PR_NUMBER, head_sha=_HEAD_SHA, auto_merge=False
    )
    runner = _runner(factory, tmp_path)

    terminal = await _execute(
        runner,
        action=NotifyHuman(message="blocking review requires a human"),
        workspace_id=workspace_id,
        tmp_path=tmp_path,
        state=MonitorState(started_at=0.0),
    )

    since, reason, status = await _read_attention(factory, workspace_id)
    assert terminal is False
    # NotifyHuman is NOT a pause: the row stays monitoring_pr.
    assert status == WorkspaceStatus.monitoring_pr.value
    assert since is not None
    assert reason == "blocking review requires a human"


@pytest.mark.unit
async def test_repeated_notify_human_keeps_since_stable_refreshes_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(
        factory, pr_number=_PR_NUMBER, head_sha=_HEAD_SHA, auto_merge=False
    )
    runner = _runner(factory, tmp_path)
    state = MonitorState(started_at=0.0)

    await _execute(
        runner,
        action=NotifyHuman(message="first escalation"),
        workspace_id=workspace_id,
        tmp_path=tmp_path,
        state=state,
    )
    first_since, first_reason, _ = await _read_attention(factory, workspace_id)

    await _execute(
        runner,
        action=NotifyHuman(message="second escalation"),
        workspace_id=workspace_id,
        tmp_path=tmp_path,
        state=state,
    )
    second_since, second_reason, _ = await _read_attention(factory, workspace_id)

    assert first_since is not None
    assert first_reason == "first escalation"
    # The episode start stays stable across repeated NotifyHuman; reason tracks latest.
    assert second_since == first_since
    assert second_reason == "second escalation"


@pytest.mark.unit
async def test_resume_with_non_notify_human_action_clears_attention(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(
        factory, pr_number=_PR_NUMBER, head_sha=_HEAD_SHA, auto_merge=False
    )
    runner = _runner(factory, tmp_path)

    await _execute(
        runner,
        action=NotifyHuman(message="blocked on human"),
        workspace_id=workspace_id,
        tmp_path=tmp_path,
        state=MonitorState(started_at=0.0),
    )
    since_before, _, _ = await _read_attention(factory, workspace_id)
    assert since_before is not None

    # The monitor resumes: decide() returns WaitForCI (a non-NotifyHuman action),
    # so the top-of-_execute clear nulls the flag.
    terminal = await _execute(
        runner,
        action=WaitForCI(reason="pending_checks"),
        workspace_id=workspace_id,
        tmp_path=tmp_path,
        state=MonitorState(started_at=0.0),
    )

    since_after, reason_after, status = await _read_attention(factory, workspace_id)
    assert terminal is False
    assert status == WorkspaceStatus.monitoring_pr.value
    assert since_after is None
    assert reason_after is None


@pytest.mark.unit
async def test_terminal_action_clears_attention_on_exit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(
        factory, pr_number=_PR_NUMBER, head_sha=_HEAD_SHA, auto_merge=False
    )
    runner = _runner(factory, tmp_path)

    await _execute(
        runner,
        action=NotifyHuman(message="blocked on human"),
        workspace_id=workspace_id,
        tmp_path=tmp_path,
        state=MonitorState(started_at=0.0),
    )
    since_before, _, _ = await _read_attention(factory, workspace_id)
    assert since_before is not None

    # ShortCircuitCompleted is a terminal, non-NotifyHuman action: exiting
    # monitoring_pr clears the attention flag too.
    terminal = await _execute(
        runner,
        action=ShortCircuitCompleted(),
        workspace_id=workspace_id,
        tmp_path=tmp_path,
        state=MonitorState(started_at=0.0),
    )

    since_after, reason_after, _ = await _read_attention(factory, workspace_id)
    assert terminal is True
    assert since_after is None
    assert reason_after is None


@pytest.mark.unit
async def test_attention_flag_persists_across_monitor_restart(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(
        factory, pr_number=_PR_NUMBER, head_sha=_HEAD_SHA, auto_merge=False
    )

    # A first runner flags the workspace.
    runner_a = _runner(factory, tmp_path)
    await _execute(
        runner_a,
        action=NotifyHuman(message="blocking review"),
        workspace_id=workspace_id,
        tmp_path=tmp_path,
        state=MonitorState(started_at=0.0),
    )
    since_first, _, _ = await _read_attention(factory, workspace_id)
    assert since_first is not None

    # Simulate a monitor restart: a brand-new runner + a fresh MonitorState with
    # no in-memory carry-over. The flag is persisted (not derived), so COALESCE
    # keeps the ORIGINAL episode start even though nothing in memory remembers it.
    runner_b = _runner(factory, tmp_path)
    await _execute(
        runner_b,
        action=NotifyHuman(message="still blocked after restart"),
        workspace_id=workspace_id,
        tmp_path=tmp_path,
        state=MonitorState(started_at=999.0),
    )
    since_restart, reason_restart, _ = await _read_attention(factory, workspace_id)
    assert since_restart == since_first
    assert reason_restart == "still blocked after restart"
