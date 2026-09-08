"""A timed-out operator hint with preserved work is retried once (issue #932).

Before #932 a blind ``AGENT_IDLE_TIMEOUT`` on an operator-hint run rolled the
agent's commit away and parked the monitor at ``NotifyHuman`` on the first
failure. The timeout now preserves the work, so the hint deserves one more
attempt before a human is called: leave it ``pending`` (``decide()`` returns
``AddressOperatorHint`` again) and record a retry marker. A *second* timeout
finds the marker and parks exactly as today, and a non-timeout ``agent_failed``
still parks immediately.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressOperatorHint,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AgentVerdictExecutionError,
    MonitorVerdictResult,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.operator_hint_timeout_retry import (
    operator_hint_timeout_retry_key,
    should_retry_timed_out_hint,
    timeout_retry_reason_code,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_PRESERVED_HEAD = "b" * 40
_OPERATION_ID = "op_timed_out_hint"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _hint() -> OperatorHint:
    return OperatorHint(
        reason="the fix-cycle stalled; finish the refactor",
        directive="finish the refactor and commit it",
        operation_id=_OPERATION_ID,
        requested_at="2026-09-06T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )


def _timeout_execution_error(reason_code: str = "AGENT_IDLE_TIMEOUT") -> AgentVerdictExecutionError:
    return AgentVerdictExecutionError(
        reason_code=reason_code,
        reason=(
            f"agent timed out ({reason_code}); preserved work at {_PRESERVED_HEAD} — "
            "retrying from the original item start"
        ),
        preserved_head_sha=_PRESERVED_HEAD,
    )


async def _run_cycle(
    runner: object,
    *,
    workspace_id: str,
    hint: OperatorHint,
    state: MonitorState,
    tmp_path: Path,
) -> object:
    return await runner._run_operator_hint_cycle(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )


def _stub_cycle_preconditions(
    runner: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("a" * 40, None)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)


@pytest.mark.unit
async def test_timed_out_hint_with_preserved_work_is_retried_once(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)
    _stub_cycle_preconditions(runner, monkeypatch)

    async def _timed_out(**_kwargs: object) -> object:
        raise _timeout_execution_error()

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _timed_out)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.pushed is False
    assert result.failed is False
    # Still pending: ``decide()`` returns AddressOperatorHint again, not NotifyHuman.
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "pending"
    assert state.threads_addressed_ids[operator_hint_timeout_retry_key(hint)] == "retried"
    # The envelope is flagged and carries the watchdog code, so the monitor loop
    # cannot record the retry as an indistinguishable succeeded no-op.
    assert result.operator_hint_timeout_retry is True
    assert result.reason_code == "AGENT_IDLE_TIMEOUT"


@pytest.mark.unit
async def test_second_timeout_parks_the_hint_for_a_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(operator_hint_timeout_retry_key(hint), "retried")
    _stub_cycle_preconditions(runner, monkeypatch)

    async def _timed_out(**_kwargs: object) -> object:
        raise _timeout_execution_error()

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _timed_out)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is False
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "agent_failed"
    assert _PRESERVED_HEAD in (state.pending_operator_hint.status_reason or "")
    # The retry budget is spent and cleared with the terminal outcome.
    assert operator_hint_timeout_retry_key(hint) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_non_timeout_agent_failure_parks_immediately(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)
    _stub_cycle_preconditions(runner, monkeypatch)

    async def _cli_failed(**_kwargs: object) -> object:
        raise AgentVerdictExecutionError(reason_code="AGENT_CLI_FAILED")

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_failed)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is False
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "agent_failed"
    assert operator_hint_timeout_retry_key(hint) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_timeout_without_preserved_work_parks_immediately(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was preserved, so a retry buys nothing — keep today's behaviour."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)
    _stub_cycle_preconditions(runner, monkeypatch)

    async def _timed_out_no_work(**_kwargs: object) -> object:
        raise AgentVerdictExecutionError(
            reason_code="AGENT_IDLE_TIMEOUT",
            reason="agent timed out (AGENT_IDLE_TIMEOUT)",
        )

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _timed_out_no_work)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is False
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "agent_failed"
    assert operator_hint_timeout_retry_key(hint) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_retry_marker_is_cleared_when_the_hint_succeeds(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(operator_hint_timeout_retry_key(hint), "retried")
    _stub_cycle_preconditions(runner, monkeypatch)

    async def _fixed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_violation(**_kwargs: object) -> None:
        return None

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return _PRESERVED_HEAD

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_violation)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.pushed is True
    assert state.pending_operator_hint is None
    assert operator_hint_timeout_retry_key(hint) not in state.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (VerdictResult(verdict="fix_committed"), False),
        (MonitorVerdictResult(verdict="needs_human"), False),
        (
            MonitorVerdictResult(
                verdict="agent_failed",
                reason_code="AGENT_CLI_FAILED",
                preserved_head_sha=_PRESERVED_HEAD,
            ),
            False,
        ),
        (
            MonitorVerdictResult(verdict="agent_failed", reason_code="AGENT_TIMEOUT"),
            False,
        ),
        (
            MonitorVerdictResult(
                verdict="agent_failed",
                reason_code="AGENT_TIMEOUT",
                preserved_head_sha=_PRESERVED_HEAD,
            ),
            True,
        ),
    ],
)
def test_should_retry_timed_out_hint_gate(
    verdict: VerdictResult | MonitorVerdictResult,
    expected: bool,
) -> None:
    """Only a timeout that actually preserved work spends the retry."""
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    assert should_retry_timed_out_hint(state, hint, verdict) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (
            MonitorVerdictResult(verdict="agent_failed", reason_code="AGENT_IDLE_TIMEOUT"),
            "AGENT_IDLE_TIMEOUT",
        ),
        (MonitorVerdictResult(verdict="agent_failed"), "AGENT_TIMEOUT"),
        (VerdictResult(verdict="fix_committed"), "AGENT_TIMEOUT"),
    ],
)
def test_timeout_retry_reason_code_carries_the_watchdog_code(
    verdict: VerdictResult | MonitorVerdictResult,
    expected: str,
) -> None:
    """A granted retry reports the watchdog code; the fallback keeps it total."""
    assert timeout_retry_reason_code(verdict) == expected


@pytest.mark.unit
async def test_retry_cycle_records_a_failed_operation_with_the_timeout_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry must not be filed as a succeeded ``operator_hint_needs_human``.

    No human wait was entered and the watchdog did fire, so operation history
    records the attempt as failed with its reason code (AGENTS.md: retries must
    preserve reason codes).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    async def _retried_cycle(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=False,
            returncode=0,
            reason_code="AGENT_IDLE_TIMEOUT",
            operator_hint_timeout_retry=True,
        )

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _retried_cycle)

    handled = await runner._execute(
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=PRStatus(
            number=42,
            head_sha="abc1234567890def",
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        ),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert handled is False
    async with factory() as session:
        operation = (
            (
                await session.execute(
                    select(Operation).where(
                        Operation.workspace_id == workspace_id,
                        Operation.type == OperationType.comment_repair.value,
                    )
                )
            )
            .scalars()
            .one()
        )

    assert operation.status == OperationStatus.failed.value
    assert operation.result["outcome"] == "operator_hint_timeout_retry"
    assert operation.result["reason_code"] == "AGENT_IDLE_TIMEOUT"
    assert operation.error_code == "AGENT_IDLE_TIMEOUT"


@pytest.mark.unit
def test_retry_key_falls_back_when_the_hint_has_no_operation_id() -> None:
    hint = OperatorHint(reason="no operation id yet", requested_at="2026-09-06T00:00:00+00:00")

    assert operator_hint_timeout_retry_key(hint).endswith(":pending")
