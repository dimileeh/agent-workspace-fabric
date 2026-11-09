"""PR-monitor work-slot release points and hook injection (slot-release).

A waiting PR monitor holds no work slot: ``loop._execute`` releases a retained
lease before every action that runs neither an agent nor a validation
(WaitForCI, NotifyHuman, Merge, ...), and ``run()`` releases it before its
outer transient-retry backoffs. The work actions (AddressComments,
AddressOperatorHint, ReportCiFailure, SyncBase) keep it, and the agent and
pre-push validation runs they launch receive the runner's injected ``work_slot``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    Abort,
    AbortReason,
    AddressComments,
    AddressOperatorHint,
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    NotifyHuman,
    OperatorHint,
    PRStatus,
    ReportCiFailure,
    RerunTransientCI,
    ShortCircuitCompleted,
    SyncBase,
    WaitForCI,
    WaitForTransientCI,
)
from awf.runtime.pr_monitor_runner import loop as monitor_loop
from awf.runtime.pr_monitor_runner.types import BaseFetchError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _coverage_result,
    _FakeValidation,
    _set_resolved_profile,
    _validation_result,
)

pytestmark = pytest.mark.unit

_PR_NUMBER = 42
_HEAD_SHA = "abc1234567890def"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@dataclass
class _RecordingWorkSlot:
    events: list[str] = field(default_factory=list)

    async def release_idle(self) -> None:
        self.events.append("release_idle")

    def hold(self, phase: str) -> Any:  # pragma: no cover - holds happen in adapters
        raise AssertionError(f"unexpected hold({phase})")


class _TimelineSleep(RecordedSleep):
    """Sleep shim appending to the injected slot's event log, so order is visible."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def __call__(self, seconds: float) -> None:
        self._events.append("sleep")
        await super().__call__(seconds)


class _SlotAwareAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.work_slots: list[object] = []

    async def run(self, *, work_slot: object = None, **kwargs: Any) -> AgentRunResult:  # type: ignore[override]
        self.work_slots.append(work_slot)
        return await super().run(**kwargs)


_PURE_WAIT_ACTIONS = [
    WaitForCI(),
    WaitForTransientCI(failures=(), wait_seconds=1.0, wait_count=1),
    RerunTransientCI(failures=()),
    NotifyHuman(message="needs a human"),
    Merge(),
    ShortCircuitCompleted(),
    Abort(reason=AbortReason.pr_closed_externally),
]
_WORK_ACTIONS = [
    AddressComments(threads=(), review_comments=()),
    AddressOperatorHint(hint=OperatorHint(reason="r", directive="do it")),
    ReportCiFailure(failures=()),
    SyncBase(),
]


@pytest.mark.parametrize("action", _PURE_WAIT_ACTIONS, ids=lambda a: type(a).__name__)
async def test_pure_wait_actions_release_the_retained_slot(action: object) -> None:
    slot = _RecordingWorkSlot()
    runner = type("_Runner", (), {"_work_slot": slot})()

    await monitor_loop._release_work_slot_before_pure_wait(runner, action)  # type: ignore[arg-type]

    assert slot.events == ["release_idle"]


@pytest.mark.parametrize("action", _WORK_ACTIONS, ids=lambda a: type(a).__name__)
async def test_work_actions_keep_the_slot_for_their_hooks(action: object) -> None:
    slot = _RecordingWorkSlot()
    runner = type("_Runner", (), {"_work_slot": slot})()

    await monitor_loop._release_work_slot_before_pure_wait(runner, action)  # type: ignore[arg-type]

    assert slot.events == []


async def test_without_an_injected_slot_nothing_is_released() -> None:
    runner = type("_Runner", (), {})()
    await monitor_loop._release_work_slot_before_pure_wait(runner, WaitForCI())


def _green_status() -> PRStatus:
    return PRStatus(
        number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.PENDING,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.BLOCKED,
    )


async def test_execute_wait_for_ci_releases_before_sleeping(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(
        factory, pr_number=_PR_NUMBER, head_sha=_HEAD_SHA
    )
    slot = _RecordingWorkSlot()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    assert runner._work_slot is None
    runner._work_slot = slot  # type: ignore[assignment]

    terminal = await runner._execute(  # type: ignore[attr-defined]
        action=WaitForCI(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=_PR_NUMBER,
        status=_green_status(),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert slot.events == ["release_idle"]
    assert sleep_fn.calls


async def test_monitor_agent_runs_receive_the_injected_work_slot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    adapter = _SlotAwareAdapter()
    adapter.queue(stdout="ok")
    adapter.queue(stdout="ok")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    kwargs = {
        "workspace_id": workspace_id,
        "compose_project": "proj",
        "compose_file": tmp_path / "compose.yml",
        "prompt": "fix the review comment",
        "log_source": "monitor",
    }

    await runner._run_monitor_agent_with_service_recovery(**kwargs)  # type: ignore[attr-defined]
    slot = _RecordingWorkSlot()
    runner._work_slot = slot  # type: ignore[assignment]
    await runner._run_monitor_agent_with_service_recovery(**kwargs)  # type: ignore[attr-defined]

    assert adapter.work_slots == [None, slot]


async def test_pre_push_validation_receives_the_injected_work_slot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id, include_coverage=True)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'e' * 40}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    validation = _FakeValidation(
        _validation_result(tmp_path, ok=True),
        coverage_result=_coverage_result(tmp_path),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    slot = _RecordingWorkSlot()
    runner._work_slot = slot  # type: ignore[assignment]

    result = await runner._validated_git_push_result(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert validation.calls[0]["work_slot"] is slot
    assert validation.coverage_calls[0]["work_slot"] is slot


def _transient_github_error() -> GitHubClientError:
    return GitHubClientError(
        operation="gh api graphql", returncode=1, stderr="HTTP 502 Bad Gateway"
    )


@pytest.mark.parametrize(
    "error",
    [
        _transient_github_error(),
        BaseFetchError("fatal: unable to access: The requested URL returned error: 503"),
    ],
    ids=["forge", "base_fetch"],
)
async def test_status_fetch_retry_wait_releases_the_retained_slot_first(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    error: Exception,
) -> None:
    """The status-fetch backoff runs before ``_execute``, so ``run()`` frees the slot."""
    workspace_id = await seed_monitoring_workspace(
        factory, pr_number=_PR_NUMBER, head_sha=_HEAD_SHA
    )
    slot = _RecordingWorkSlot()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=_TimelineSleep(slot.events),
        worktrees_root=tmp_path / "worktrees",
        max_outer_iterations=1,
    )
    mocker.patch.object(runner, "_fetch_status_for_decision", mocker.AsyncMock(side_effect=error))

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        work_slot=slot,  # type: ignore[arg-type]
    )

    assert slot.events == ["release_idle", "sleep"]


async def test_execute_retry_wait_releases_the_retained_slot_first(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """A forge fault escaping a work action's ``_execute`` frees the slot before backoff."""
    workspace_id = await seed_monitoring_workspace(
        factory, pr_number=_PR_NUMBER, head_sha=_HEAD_SHA
    )
    slot = _RecordingWorkSlot()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=_TimelineSleep(slot.events),
        worktrees_root=tmp_path / "worktrees",
        max_outer_iterations=1,
    )
    mocker.patch.object(
        runner, "_fetch_status_for_decision", mocker.AsyncMock(return_value=_green_status())
    )
    mocker.patch.object(runner, "_execute", mocker.AsyncMock(side_effect=_transient_github_error()))

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        work_slot=slot,  # type: ignore[arg-type]
    )

    assert slot.events == ["release_idle", "sleep"]
