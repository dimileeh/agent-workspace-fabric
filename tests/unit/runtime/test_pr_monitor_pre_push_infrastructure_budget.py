"""Bound repeated infrastructure-signature ``PRE_PUSH_VALIDATION_FIX_FAILED`` (#1182).

A pre-push validation command that fails for an execution-context reason (shell
statuses 126 / >=128, e.g. ``git diff`` exiting 129 outside a work tree) cannot
be fixed by the fix-pass agent. Before this budget the comment-repair arm
re-ran the whole batch — fresh agents each time — on every poll forever. After a
small, fixed number of identical failures on an unchanged PR head the workspace
must park for a human with the evidence preserved, and no further agent runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
    AddressOperatorHint,
    MonitorState,
    OperatorHint,
    ReviewThread,
    WaitForCI,
)
from awf.runtime.pr_monitor_runner import pre_push_infrastructure_budget as budget
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_INFRASTRUCTURE_FAILURE_PARK_AFTER,
    _PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
    _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILURE_PARKED_REASON,
)
from awf.runtime.pr_monitor_runner.remote_ops import _git_push_failure_outcome, _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_runner_parts.test_pr_monitor_runner_part_004 import (
    _green_status,
)

_PARKED = _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILURE_PARKED_REASON
_PRE_PUSH_VALIDATION_FAILED = "PRE_PUSH_VALIDATION_FAILED"
_CAP = _PRE_PUSH_INFRASTRUCTURE_FAILURE_PARK_AFTER
_HEAD = "a" * 40


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _fix_failed(
    *,
    returncode: object = 129,
    command: str = "git diff --check",
    reason_code: str = _PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
    validation_run_id: str = "vr_1",
) -> _GitPushResult:
    """Build the envelope ``_validated_git_push_result`` returns after a failed fix pass."""
    details: dict[str, object] = {
        "phase": "pre_push_validation",
        "reason_code": reason_code,
        "validation_reason_code": "COMMAND_FAILED",
        "validation_run_id": validation_run_id,
        "failing_command": command,
        "pushed": False,
    }
    if returncode is not None:
        details["failing_returncode"] = returncode
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="PR monitor pre-push validation fix pass failed after 1/1 attempts",
        reason_code=reason_code,
        details=details,
    )


# ── Pure helpers ───────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("returncode", [126, 128, 129, 137, -9])
def test_signature_is_set_for_execution_context_failures(returncode: int) -> None:
    """Shell-reserved statuses (not executable, usage, signal) are infrastructure."""
    signature = budget.pre_push_infrastructure_failure_signature(
        _fix_failed(returncode=returncode), pr_head_sha=_HEAD
    )
    assert signature is not None
    assert len(signature) == 12


@pytest.mark.unit
@pytest.mark.parametrize(
    "returncode", [1, 2, 5, 127, 132, 133, 134, 135, 136, 139, 159, -6, -11, None, True, "129"]
)
def test_signature_is_none_for_product_failures_and_unknown_codes(returncode: object) -> None:
    """Genuine lint/test failures, crashes on a fault signal (SIGSEGV 139 / -11,
    SIGABRT 134 / -6, ...), toolchain-missing 127, and missing codes never park."""
    assert (
        budget.pre_push_infrastructure_failure_signature(
            _fix_failed(returncode=returncode), pr_head_sha=_HEAD
        )
        is None
    )


@pytest.mark.unit
def test_signature_is_none_for_other_reason_codes_and_successes() -> None:
    """Only pre-push validation failures (with or without a fix pass) are budgeted."""
    other = _fix_failed(reason_code="PRE_PUSH_VALIDATION_ROLLBACK_FAILED")
    ok = _GitPushResult(pushed=True, failed=False, returncode=0)
    assert budget.pre_push_infrastructure_failure_signature(other, pr_head_sha=_HEAD) is None
    assert budget.pre_push_infrastructure_failure_signature(ok, pr_head_sha=_HEAD) is None
    no_details = replace(_fix_failed(), details=None)
    assert budget.pre_push_infrastructure_failure_signature(no_details, pr_head_sha=_HEAD) is None


@pytest.mark.unit
def test_fix_failed_and_validation_failed_share_one_signature() -> None:
    """A fix pass that commits a guess surfaces the same rc-129 failure as
    ``PRE_PUSH_VALIDATION_FAILED``; alternating codes must not reset the budget."""
    state = MonitorState(started_at=0.0)
    record = budget.record_pre_push_infrastructure_failure
    after_commit = _fix_failed(reason_code=_PRE_PUSH_VALIDATION_FAILED)
    assert record(state, _fix_failed(), pr_head_sha=_HEAD) == 1
    assert record(state, after_commit, pr_head_sha=_HEAD) == 2
    assert record(state, _fix_failed(), pr_head_sha=_HEAD) == 3

    parked = budget.pre_push_infrastructure_park_result(state, pr_head_sha=_HEAD)

    assert parked is not None
    assert parked.details is not None
    assert parked.details["underlying_reason_code"] == _PRE_PUSH_VALIDATION_FIX_FAILED_REASON


@pytest.mark.unit
def test_record_counts_same_signature_and_resets_on_change() -> None:
    """Same signature increments; a new command, return code or PR head restarts at 1."""
    state = MonitorState(started_at=0.0)
    record = budget.record_pre_push_infrastructure_failure
    status_cmd = "git status --short"
    assert record(state, _fix_failed(), pr_head_sha=_HEAD) == 1
    assert record(state, _fix_failed(validation_run_id="vr_2"), pr_head_sha=_HEAD) == 2
    assert record(state, _fix_failed(command=status_cmd), pr_head_sha=_HEAD) == 1
    assert record(state, _fix_failed(command=status_cmd), pr_head_sha=_HEAD) == 2
    rc_128 = _fix_failed(command=status_cmd, returncode=128)
    assert record(state, rc_128, pr_head_sha=_HEAD) == 1
    assert record(state, rc_128, pr_head_sha="b" * 40) == 1


@pytest.mark.unit
def test_record_clears_on_genuine_failure_and_success() -> None:
    """A product failure or a successful push ends the infrastructure episode."""
    state = MonitorState(started_at=0.0)
    record = budget.record_pre_push_infrastructure_failure
    record(state, _fix_failed(), pr_head_sha=_HEAD)
    assert state.pre_push_infra_failure is not None
    assert record(state, _fix_failed(returncode=1), pr_head_sha=_HEAD) == 0
    assert state.pre_push_infra_failure is None
    record(state, _fix_failed(), pr_head_sha=_HEAD)
    ok = _GitPushResult(pushed=True, failed=False, returncode=0)
    assert record(state, ok, pr_head_sha=_HEAD) == 0
    assert state.pre_push_infra_failure is None


@pytest.mark.unit
def test_park_result_requires_cap_and_matching_head() -> None:
    """The gate engages only at the cap and only for the PR head it was counted on."""
    state = MonitorState(started_at=0.0)
    for attempt in range(1, _CAP):
        budget.record_pre_push_infrastructure_failure(
            state, _fix_failed(validation_run_id=f"vr_{attempt}"), pr_head_sha=_HEAD
        )
        assert budget.pre_push_infrastructure_park_result(state, pr_head_sha=_HEAD) is None
        assert budget.pre_push_infrastructure_park_active(state, pr_head_sha=_HEAD) is False
    budget.record_pre_push_infrastructure_failure(
        state, _fix_failed(validation_run_id=f"vr_{_CAP}"), pr_head_sha=_HEAD
    )

    parked = budget.pre_push_infrastructure_park_result(state, pr_head_sha=_HEAD)

    assert parked is not None
    assert parked.failed is True
    assert parked.parked_needs_human is True
    assert parked.terminal_monitor_failure is False
    assert parked.reason_code == _PARKED
    assert _git_push_failure_outcome(parked) == "pre_push_validation_infrastructure_parked"
    assert parked.details is not None
    assert parked.details["failing_command"] == "git diff --check"
    assert parked.details["failing_returncode"] == 129
    assert parked.details["validation_run_id"] == f"vr_{_CAP}"
    assert parked.details["validation_reason_code"] == "COMMAND_FAILED"
    assert parked.details["underlying_reason_code"] == _PRE_PUSH_VALIDATION_FIX_FAILED_REASON
    assert parked.details["attempts"] == _CAP
    assert parked.details["cap"] == _CAP
    assert parked.details["pr_head_sha"] == _HEAD
    assert "git diff --check" in (parked.error_message or "")
    assert budget.pre_push_infrastructure_park_active(state, pr_head_sha=_HEAD) is True
    # A human push moves the PR head: the gate lifts.
    assert budget.pre_push_infrastructure_park_result(state, pr_head_sha="b" * 40) is None
    assert budget.pre_push_infrastructure_park_active(state, pr_head_sha="b" * 40) is False


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["not json", "[]", '{"count": "x"}', '{"count": 9}'])
def test_malformed_persisted_record_is_ignored(raw: str) -> None:
    """A corrupted reserved value must not crash the monitor or engage the gate."""
    state = MonitorState(started_at=0.0)
    state.mark_pre_push_infra_failure(raw)
    assert budget.pre_push_infrastructure_park_result(state, pr_head_sha=_HEAD) is None
    assert budget.pre_push_infrastructure_park_active(state, pr_head_sha=_HEAD) is False
    record = budget.record_pre_push_infrastructure_failure
    assert record(state, _fix_failed(), pr_head_sha=_HEAD) == 1


@pytest.mark.unit
async def test_budget_state_round_trips_through_persistence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The reserved key survives ``_persist_state`` + ``_load_state`` (worker restart)."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState(started_at=0.0)
    for _ in range(_CAP):
        budget.record_pre_push_infrastructure_failure(state, _fix_failed(), pr_head_sha=_HEAD)

    await runner._persist_state(workspace_id, state)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    restored = runner._load_state(ws)

    assert restored.pre_push_infra_failure == state.pre_push_infra_failure
    assert budget.pre_push_infrastructure_park_result(restored, pr_head_sha=_HEAD) is not None


# ── Monitor loop ───────────────────────────────────────────────────────────


def _thread() -> ReviewThread:
    """Build the unresolved review thread that holds ``decide()`` on AddressComments."""
    return ReviewThread(
        thread_id="PRRT_infra",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )


class _CycleBody:
    """Stand in for the fix-cycle body that launches comment-repair agents.

    It is patched over ``_pre_existing_dirty_repair_worktree_result`` — the first
    call after the budget gate in ``_run_fix_cycle`` — so every invocation is one
    cycle that would have run agents, and returns what the validated push did.
    """

    def __init__(self, *results: _GitPushResult) -> None:
        """Queue the per-cycle push results; the last one repeats."""
        self.results = list(results)
        self.calls = 0

    async def __call__(self, **_kwargs: object) -> _GitPushResult:
        """Count one agent-running cycle and return its push result."""
        self.calls += 1
        return self.results[min(self.calls, len(self.results)) - 1]


async def _poll(
    runner: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    tmp_path: Path,
    head_sha: str = _HEAD,
    action: object | None = None,
) -> bool:
    """Run one monitor poll's action through the real ``_execute``."""
    thread = _thread()
    status = replace(_green_status(), head_sha=head_sha, unresolved_inline_threads=(thread,))
    return bool(
        await runner._execute(
            action=action or AddressComments(threads=(thread,), review_comments=()),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=status,
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )
    )


def _runner(factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> Any:
    """Build a monitor runner with scripted command/adapter fakes."""
    return make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )


async def _park_events(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> list[Any]:
    """Return the workspace events that carry the infrastructure park reason."""
    async with factory() as session:
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id, limit=200)
    return [event for event in events if event.reason_code == _PARKED]


@pytest.mark.unit
async def test_repeated_infra_fix_failed_parks_after_cap_without_more_agent_runs(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """The #1182 loop: identical rc-129 fix failures park after the cap; agents stop."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path)
    body = _CycleBody(_fix_failed())
    mocker.patch.object(runner, "_pre_existing_dirty_repair_worktree_result", body)
    state = MonitorState(started_at=0.0)

    for _ in range(_CAP + 3):
        ended = await _poll(runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path)
        assert ended is False

    assert body.calls == _CAP
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    assert isinstance(ws.awaiting_human_since, datetime)
    assert "git diff --check" in (ws.awaiting_human_reason or "")
    # Every retry replays the batch's idempotent comment-repair operation, so the
    # row keeps the first cycle's failure; the park is evidenced by its event.
    assert [op.error_code for op in operations] == [_PRE_PUSH_VALIDATION_FIX_FAILED_REASON]
    assert operations[0].status == OperationStatus.failed.value
    events = await _park_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0].payload["failing_command"] == "git diff --check"
    assert events[0].payload["failing_returncode"] == 129
    assert events[0].payload["validation_run_id"] == "vr_1"
    assert events[0].payload["underlying_reason_code"] == _PRE_PUSH_VALIDATION_FIX_FAILED_REASON
    assert events[0].payload["attempts"] == _CAP
    assert events[0].event_type == "monitor.pre_push_validation_infrastructure_parked"


@pytest.mark.unit
async def test_park_keeps_one_attention_episode_across_polls(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """The parked wait must not restart ``awaiting_human_since`` on every poll."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path)
    mocker.patch.object(
        runner, "_pre_existing_dirty_repair_worktree_result", _CycleBody(_fix_failed())
    )
    state = MonitorState(started_at=0.0)
    for _ in range(_CAP):
        await _poll(runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path)
    async with factory() as session:
        first = await WorkspaceRepository(session).get(workspace_id)
    assert first is not None
    assert isinstance(first.awaiting_human_since, datetime)

    await _poll(runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path)

    async with factory() as session:
        second = await WorkspaceRepository(session).get(workspace_id)
    assert second is not None
    assert second.awaiting_human_since == first.awaiting_human_since


@pytest.mark.unit
@pytest.mark.parametrize("returncode", [1, 139])
async def test_repeated_genuine_fix_failed_keeps_retrying(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    returncode: int,
) -> None:
    """A real product failure (exit 1, or a project test segfaulting with 139)
    keeps today's retry behavior and never parks."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path)
    body = _CycleBody(_fix_failed(returncode=returncode, command="pytest -q"))
    mocker.patch.object(runner, "_pre_existing_dirty_repair_worktree_result", body)
    state = MonitorState(started_at=0.0)

    for _ in range(_CAP + 2):
        await _poll(runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path)

    assert body.calls == _CAP + 2
    assert state.pre_push_infra_failure is None
    assert await _park_events(factory, workspace_id) == []


@pytest.mark.unit
async def test_infra_park_lifts_when_pr_head_changes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """A push to the PR (new remote head) lets the next cycle run agents again."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path)
    ok = _GitPushResult(pushed=True, failed=False, returncode=0)
    body = _CycleBody(*([_fix_failed()] * _CAP), ok)
    mocker.patch.object(runner, "_pre_existing_dirty_repair_worktree_result", body)
    state = MonitorState(started_at=0.0)
    for _ in range(_CAP + 1):
        await _poll(runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path)
    assert body.calls == _CAP

    await _poll(
        runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path, head_sha="b" * 40
    )

    assert body.calls == _CAP + 1
    assert state.pre_push_infra_failure is None


@pytest.mark.unit
async def test_infra_park_survives_other_actions_on_the_same_pr_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """An automatic non-repair poll (e.g. a SyncBase that did not push) keeps the park.

    Leaving the repair arm retires the attention flag, but the budget stays tied to
    the unchanged PR head, so the next comment-repair poll must not run agents.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path)
    body = _CycleBody(_fix_failed())
    mocker.patch.object(runner, "_pre_existing_dirty_repair_worktree_result", body)
    state = MonitorState(started_at=0.0)
    for _ in range(_CAP):
        await _poll(runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path)
    parked_record = state.pre_push_infra_failure
    assert parked_record is not None

    await _poll(
        runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path, action=WaitForCI()
    )

    assert state.pre_push_infra_failure == parked_record
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    assert ws.awaiting_human_since is None

    await _poll(runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path)

    assert body.calls == _CAP
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    assert isinstance(ws.awaiting_human_since, datetime)


@pytest.mark.unit
async def test_operator_guide_lifts_the_park_without_a_new_pr_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """An operator guide is explicit re-engagement: it resets the budget even when
    its cycle pushes nothing, so the next comment-repair poll retries validation."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path)
    body = _CycleBody(_fix_failed())
    mocker.patch.object(runner, "_pre_existing_dirty_repair_worktree_result", body)
    state = MonitorState(started_at=0.0)
    for _ in range(_CAP + 1):
        await _poll(runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path)
    assert body.calls == _CAP

    async def _hint_cycle_without_push(**kwargs: Any) -> _GitPushResult:
        kwargs["state"].pending_operator_hint = None
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    mocker.patch.object(runner, "_run_operator_hint_cycle", _hint_cycle_without_push)
    hint = OperatorHint(reason="validation env fixed", directive="retry", operation_id="op_1")
    state.pending_operator_hint = hint
    await _poll(
        runner,
        workspace_id=workspace_id,
        state=state,
        tmp_path=tmp_path,
        action=AddressOperatorHint(hint=hint),
    )

    assert state.pre_push_infra_failure is None
    await _poll(runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path)
    assert body.calls == _CAP + 1


@pytest.mark.unit
async def test_park_event_append_failure_is_retried_on_next_parked_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped best-effort event write is not marked recorded, so it is retried."""
    appended: list[dict[str, object]] = []
    outcomes = [False, True]

    async def _append(_runner: object, **kwargs: Any) -> bool:
        appended.append(dict(kwargs))
        return outcomes.pop(0)

    monkeypatch.setattr(budget, "_append_disposition_event", _append)
    state = MonitorState(started_at=0.0)
    for _ in range(_CAP - 1):
        budget.record_pre_push_infrastructure_failure(state, _fix_failed(), pr_head_sha=_HEAD)

    first = await budget.bound_pre_push_infrastructure_failure(
        object(), workspace_id="ws", state=state, push_result=_fix_failed(), pr_head_sha=_HEAD
    )
    gate = budget.pre_push_infrastructure_park_result(state, pr_head_sha=_HEAD)
    assert gate is not None
    second = await budget.bound_pre_push_infrastructure_failure(
        object(), workspace_id="ws", state=state, push_result=gate, pr_head_sha=_HEAD
    )
    third = await budget.bound_pre_push_infrastructure_failure(
        object(), workspace_id="ws", state=state, push_result=gate, pr_head_sha=_HEAD
    )

    assert first.reason_code == second.reason_code == third.reason_code == _PARKED
    assert len(appended) == 2
    assert appended[1]["reason_code"] == _PARKED


@pytest.mark.unit
async def test_repeated_infra_validation_failed_without_fix_pass_parks_after_cap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """With fix passes disabled (or after a committed guess) the same rc-129 failure
    arrives as ``PRE_PUSH_VALIDATION_FAILED``; it is bounded the same way."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path)
    body = _CycleBody(_fix_failed(reason_code=_PRE_PUSH_VALIDATION_FAILED))
    mocker.patch.object(runner, "_pre_existing_dirty_repair_worktree_result", body)
    state = MonitorState(started_at=0.0)

    for _ in range(_CAP + 2):
        await _poll(runner, workspace_id=workspace_id, state=state, tmp_path=tmp_path)

    assert body.calls == _CAP
    events = await _park_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0].payload["underlying_reason_code"] == _PRE_PUSH_VALIDATION_FAILED
