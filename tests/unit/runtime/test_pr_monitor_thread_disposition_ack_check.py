"""The runner step that asks the agent about a bot reply after AWF's marker (#1138, D1).

The gate only flags a thread whose opening bot replied after AWF's disposition;
the next poll's runner step, before ``decide()`` and never under the merge lock,
re-reads that thread and runs one judgment-only agent check per comment
identity. The judgment is persisted before anything else happens, so a restart
cannot re-run it; the gate then certifies an acknowledgement, or the fallback
reopens the thread.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.forge_thread_audit import ReviewThreadAudit
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import (
    _disposition_ack_key,
    _disposition_ack_pending_key,
    _disposition_auto_reopen_key,
)
from awf.runtime.pr_monitor import MonitorState, NotifyHuman
from awf.runtime.pr_monitor_runner.lifecycle import _load_state
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    record_item_disposition,
)
from awf.runtime.pr_monitor_runner.thread_disposition_ack import (
    ACKNOWLEDGES,
    NEW_POINT,
    UNJUDGED,
    comment_identity,
)
from awf.runtime.pr_monitor_runner.thread_disposition_ack_check import (
    judge_pending_disposition_acks,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
)
from awf.service.pr_monitor_adoption_seed import seedable_monitor_state
from tests.postgres import postgres_test_engine
from tests.unit.runtime._thread_disposition_fixtures import (
    BOT_ACK,
    BOT_FEEDBACK,
    BOUND_DIGEST,
    acked_thread,
    settled_baseline_state,
    thread_comment,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_gate import (
    _AUDIT,
    _REPO,
    _SILENT,
    _VIEWER,
    _execute,
    _gate,
    _marker,
    _release_runner,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_gate_merge import (
    _LockLog,
    _merge_under_lock_log,
    _SequencedAuditMergeClient,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_reopen import (
    ReopenForge,
    audit_of,
    handled_state,
)

pytestmark = pytest.mark.unit

_MARKER = _marker("false_positive", _SILENT)
_ACKNOWLEDGED = "REVIEW_THREAD_DISPOSITION_ACKNOWLEDGED"
_NEW_POINT = "REVIEW_THREAD_DISPOSITION_ACK_NEW_POINT"
_CHECK_FAILED = "REVIEW_THREAD_DISPOSITION_ACK_CHECK_FAILED"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _CountingForge(ReopenForge):
    """``ReopenForge`` that counts audit reads and can fail the per-thread re-read."""

    def __init__(self, *args: Any, refetch_error: Exception | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.refetches: list[str | None] = []
        self.refetch_error = refetch_error

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int, retry: bool = True, thread_id: str | None = None
    ) -> ReviewThreadAudit:
        self.refetches.append(thread_id)
        if thread_id is not None and self.refetch_error is not None:
            raise self.refetch_error
        return await super().fetch_review_thread_audit(
            repo=repo, pr_number=pr_number, retry=retry, thread_id=thread_id
        )


async def _judge(runner: Any, workspace_id: str, state: MonitorState) -> None:
    await judge_pending_disposition_acks(
        runner,
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        state=state,
        compose_project="proj",
        compose_file=Path("/tmp/compose.yml"),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        monitor_log=None,
    )


async def _persisted(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> MonitorState:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    return _load_state(None, ws)


async def _ack_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[dict[str, Any]]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == workspace_id, WorkspaceEvent.event_type == _AUDIT)
            .order_by(WorkspaceEvent.event_order)
        )
        return [
            dict(row.payload)
            for row in rows.scalars().all()
            if row.payload.get("action") == "review_thread_disposition_ack_check"
        ]


async def _flagged(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, **thread_kwargs: Any
) -> tuple[MonitorState, _CountingForge, str, Any]:
    """A handled thread the gate flagged for an ack check on the last poll."""
    state = handled_state()
    gh = _CountingForge(
        FakeCommandRunner(), audit=audit_of(acked_thread(_SILENT, _MARKER, **thread_kwargs))
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    assert (await _gate(runner, workspace_id, state)).retry is True
    assert _disposition_ack_pending_key(_SILENT) in state.threads_addressed_ids
    gh.refetches.clear()
    return state, gh, workspace_id, runner


def _ack_key() -> str:
    return _disposition_ack_key(_SILENT, comment_identity(thread_comment(BOT_ACK, comment_id="3")))


async def test_acknowledges_is_persisted_logged_audited_and_then_certified(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state, gh, workspace_id, runner = await _flagged(factory, tmp_path)
    runner._deps.adapter.queue(stdout="It agrees.\nAWF-DISPOSITION-ACK: ACKNOWLEDGES\n")

    with structlog.testing.capture_logs() as captured:
        await _judge(runner, workspace_id, state)

    assert gh.refetches == [_SILENT]
    [prompt] = runner._deps.adapter.calls
    assert BOT_FEEDBACK in prompt and BOT_ACK in prompt and "class=false_positive" in prompt
    persisted = await _persisted(factory, workspace_id)
    assert persisted.threads_addressed_ids[_ack_key()] == ACKNOWLEDGES
    assert _disposition_ack_pending_key(_SILENT) not in state.threads_addressed_ids
    logs = [r for r in captured if r["event"] == "monitor.review_thread_disposition_ack_check"]
    assert [(r["outcome"], r["reason_code"]) for r in logs] == [("acknowledged", _ACKNOWLEDGED)]
    [event] = await _ack_events(factory, workspace_id)
    assert (event["outcome"], event["reason_code"]) == ("acknowledged", _ACKNOWLEDGED)
    assert event["evidence"]["thread_id"] == _SILENT
    assert event["evidence"]["comment_id"] == "3"
    assert event["evidence"]["comment_identity"] == comment_identity(
        thread_comment(BOT_ACK, comment_id="3")
    )
    assert BOT_ACK not in json.dumps(event) and BOT_FEEDBACK not in json.dumps(event)
    assert gh.thread_replies == [] and gh.unresolved == []

    certified = await _gate(runner, workspace_id, state)

    assert certified.count_rows is not None
    assert dict(certified.count_rows)["false positive"] == 1
    # Later polls never ask again: the gate drops the pending key, the step is a no-op.
    await _judge(runner, workspace_id, state)
    assert len(runner._deps.adapter.calls) == 1


def _new_point(runner: Any, cmd: FakeCommandRunner) -> None:
    del cmd
    runner._deps.adapter.queue(stdout="AWF-DISPOSITION-ACK: NEW_POINT")


def _agent_failure(runner: Any, cmd: FakeCommandRunner) -> None:
    del cmd
    failure = CommandResult(returncode=1, stdout="", stderr="x")
    runner._deps.adapter.queue(exc=AgentRunError(agent="claude_code", result=failure))


def _unparseable(runner: Any, cmd: FakeCommandRunner) -> None:
    del cmd
    runner._deps.adapter.queue(stdout="I think it agrees.")


def _worktree_changed(runner: Any, cmd: FakeCommandRunner) -> None:
    runner._deps.adapter.queue(stdout="AWF-DISPOSITION-ACK: ACKNOWLEDGES")
    cmd.respond_when(
        lambda args: "status" in args and bool(runner._deps.adapter.calls),
        stdout=" M widgets/core.py\n",
    )


@pytest.mark.parametrize(
    ("script", "judgment", "outcome", "reason_code"),
    [
        pytest.param(_new_point, NEW_POINT, "new_point", _NEW_POINT, id="new-point"),
        pytest.param(_agent_failure, UNJUDGED, "check_failed", _CHECK_FAILED, id="agent-failure"),
        pytest.param(_unparseable, UNJUDGED, "check_failed", _CHECK_FAILED, id="unparseable"),
        pytest.param(_worktree_changed, UNJUDGED, "check_failed", _CHECK_FAILED, id="worktree"),
    ],
)
async def test_anything_but_acknowledges_is_recorded_and_takes_the_reopen_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    script: Any,
    judgment: str,
    outcome: str,
    reason_code: str,
) -> None:
    state, gh, workspace_id, runner = await _flagged(factory, tmp_path)
    script(runner, gh._runner)

    with structlog.testing.capture_logs() as captured:
        await _judge(runner, workspace_id, state)

    persisted = await _persisted(factory, workspace_id)
    assert persisted.threads_addressed_ids[_ack_key()] == judgment
    logs = [r for r in captured if r["event"] == "monitor.review_thread_disposition_ack_check"]
    assert [(r["outcome"], r["reason_code"]) for r in logs] == [(outcome, reason_code)]
    assert [e["outcome"] for e in await _ack_events(factory, workspace_id)] == [outcome]

    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert gh.unresolved == [_SILENT]
    assert state.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == "1"
    assert len(runner._deps.adapter.calls) == 1


async def test_the_judgment_survives_a_restart_and_is_never_asked_again(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state, _, workspace_id, runner = await _flagged(factory, tmp_path)
    runner._deps.adapter.queue(stdout="AWF-DISPOSITION-ACK: ACKNOWLEDGES")
    await _judge(runner, workspace_id, state)

    # A restart that lost the in-memory state: the persisted judgment holds, so
    # the reloaded step asks nothing (an empty adapter queue fails on any call)
    # and the gate certifies.
    reloaded = await _persisted(factory, workspace_id)
    reloaded.mark_addressed(_disposition_ack_pending_key(_SILENT), "0")
    await _judge(runner, workspace_id, reloaded)
    certified = await _gate(runner, workspace_id, reloaded)

    assert certified.count_rows is not None and certified.blocker is None
    assert len(runner._deps.adapter.calls) == 1


async def test_without_pending_keys_the_step_calls_neither_forge_nor_agent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _CountingForge(FakeCommandRunner(), audit=audit_of(acked_thread(_SILENT, _MARKER)))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _judge(runner, workspace_id, handled_state())

    assert gh.refetches == [] and runner._deps.adapter.calls == []


async def test_a_thread_no_longer_eligible_on_the_re_read_is_not_judged(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state, gh, workspace_id, runner = await _flagged(factory, tmp_path)
    human = thread_comment("Please also cover empty input.", comment_id="4", is_bot=False)
    gh.audit = audit_of(
        acked_thread(_SILENT, _MARKER, trailing=(thread_comment(BOT_ACK, comment_id="3"), human))
    )

    await _judge(runner, workspace_id, state)

    assert gh.refetches == [_SILENT]
    assert runner._deps.adapter.calls == []
    assert _disposition_ack_pending_key(_SILENT) not in state.threads_addressed_ids
    assert _ack_key() not in state.threads_addressed_ids


async def test_a_re_read_fault_keeps_the_check_pending_for_the_next_poll(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state, gh, workspace_id, runner = await _flagged(factory, tmp_path)
    gh.refetch_error = GitHubClientError(
        operation="fetch_review_thread_audit", returncode=1, stderr="HTTP 502 Bad Gateway"
    )

    with structlog.testing.capture_logs() as captured:
        await _judge(runner, workspace_id, state)

    assert runner._deps.adapter.calls == []
    assert state.threads_addressed_ids[_disposition_ack_pending_key(_SILENT)] == "1"
    assert _ack_key() not in state.threads_addressed_ids
    [log] = [
        r for r in captured if r["event"] == "monitor.review_thread_disposition_ack_refetch_failed"
    ]
    assert log["reason_code"] == _CHECK_FAILED
    assert log["forge_reason_code"] == "GITHUB_API_ERROR"
    assert log["refetch_failures"] == 1


async def test_the_in_lock_audit_never_calls_the_agent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # The pre-lock audit is clean; the bot replies before the in-lock re-audit.
    # The merge waits a poll outside the lock; only the poll step may ask the
    # agent (the merge helper's FakeAdapter has an empty queue: a call fails).
    lock = _LockLog()
    state = settled_baseline_state()
    state.mark_addressed(_SILENT, "false_positive")
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=_SILENT,
            disposition_class="false_positive",
            verdict="false_positive",
            reason="the caller already rejects None payloads",
            reply_comment_id="reply-1",
            resolved=True,
            conversation_digest=BOUND_DIGEST,
        ),
    )
    thread = acked_thread(_SILENT, _MARKER)
    assert thread.without_trailing is not None
    gh = _SequencedAuditMergeClient(
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(thread.without_trailing,)),
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(thread,)),
    )

    terminal, state, lock, _ = await _merge_under_lock_log(
        factory, tmp_path, gh, state=state, lock=lock
    )

    assert terminal is False
    assert gh.merge_calls == [] and gh.unresolved_threads == []
    assert gh.status_refreshes_at_audit == [0, 1]
    assert lock.sleeps_under_lock == [False]
    assert state.threads_addressed_ids[_disposition_ack_pending_key(_SILENT)] == "0"


@pytest.mark.parametrize("head_continuity", [True, False])
def test_an_acknowledgement_crosses_re_adoption_regardless_of_head(head_continuity: bool) -> None:
    predecessor = handled_state()
    predecessor.mark_addressed(_ack_key(), ACKNOWLEDGES)
    predecessor.mark_addressed(_disposition_ack_pending_key(_SILENT), "pending")

    seeded = seedable_monitor_state(
        predecessor.threads_addressed_ids, head_continuity=head_continuity
    )

    assert seeded[_ack_key()] == ACKNOWLEDGES
    assert _disposition_ack_pending_key(_SILENT) not in seeded


async def test_the_successor_certifies_a_crossed_acknowledgement_without_asking_again(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    predecessor = handled_state()
    predecessor.mark_addressed(_ack_key(), ACKNOWLEDGES)
    adopted = MonitorState()
    for key, value in seedable_monitor_state(
        predecessor.threads_addressed_ids, head_continuity=True
    ).items():
        adopted.mark_addressed(key, value)
    gh = _CountingForge(FakeCommandRunner(), audit=audit_of(acked_thread(_SILENT, _MARKER)))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    certified = await _gate(runner, workspace_id, adopted)
    await _judge(runner, workspace_id, adopted)

    assert certified.count_rows is not None
    assert dict(certified.count_rows)["false positive"] == 1
    assert runner._deps.adapter.calls == [] and gh.refetches == [None]


@pytest.mark.parametrize("provider_action", ["retry", "fallback", "auth_failed"])
async def test_a_provider_failure_goes_to_provider_recovery_and_judges_nothing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_action: str,
) -> None:
    # A 429, quota or auth failure is the provider's, not the reply's: it takes
    # the monitor's provider recovery, and the check stays pending for later.
    state, gh, workspace_id, runner = await _flagged(factory, tmp_path)
    failure = CommandResult(returncode=1, stdout="", stderr="429")
    runner._deps.adapter.queue(exc=AgentRunError(agent="claude_code", result=failure))
    recorded: list[str] = []

    async def provider(workspace_id: str, exc: AgentRunError) -> str:
        recorded.append(exc.reason_code)
        return provider_action

    monkeypatch.setattr(runner, "_record_provider_agent_run_error", provider)

    with pytest.raises(
        (ProviderRecoveryRetryError, ProviderRecoveryFallbackError, ProviderRecoveryAuthError)
    ):
        await _judge(runner, workspace_id, state)

    assert recorded == ["AGENT_CLI_FAILED"]
    assert _ack_key() not in state.threads_addressed_ids
    assert _ack_key() not in (await _persisted(factory, workspace_id)).threads_addressed_ids
    assert _disposition_ack_pending_key(_SILENT) in state.threads_addressed_ids
    assert await _ack_events(factory, workspace_id) == []
    assert gh.unresolved == []


async def test_a_provider_cooldown_judges_nothing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _, workspace_id, runner = await _flagged(factory, tmp_path)

    async def suppressed(**kwargs: Any) -> Any:
        raise ProviderRecoveryRetryError()

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", suppressed)

    with pytest.raises(ProviderRecoveryRetryError):
        await _judge(runner, workspace_id, state)

    assert _ack_key() not in (await _persisted(factory, workspace_id)).threads_addressed_ids
    assert _disposition_ack_pending_key(_SILENT) in state.threads_addressed_ids


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            ComposeExecCleanupError(
                invocation_id="i-1", source="agent", label="claude", message="still running"
            ),
            id="compose-cleanup",
        ),
        pytest.param(_MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING"), id="head-object"),
        pytest.param(_MonitorMirrorHooksPathRepairFailedError(), id="mirror-hooks"),
        pytest.param(_MonitorAgentRuntimeOwnershipRepairFailedError("chown"), id="ownership"),
        pytest.param(TimeoutError(), id="timeout"),
    ],
)
async def test_an_agent_infrastructure_failure_is_a_failed_check(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    state, gh, workspace_id, runner = await _flagged(factory, tmp_path)

    async def failing(**kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", failing)

    await _judge(runner, workspace_id, state)

    persisted = await _persisted(factory, workspace_id)
    assert persisted.threads_addressed_ids[_ack_key()] == UNJUDGED
    [event] = await _ack_events(factory, workspace_id)
    assert event["outcome"] == "check_failed"
    assert event["evidence"]["detail"] == f"agent_failed:{type(error).__name__}"
    await _execute(runner, workspace_id, NotifyHuman(), state)
    assert gh.unresolved == [_SILENT]


async def test_a_crash_during_the_check_never_runs_it_twice(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The check is recorded as failed before the agent starts, so an unforeseen
    # crash mid-run cannot make a resumed monitor run it again.
    state, _, workspace_id, runner = await _flagged(factory, tmp_path)
    runs: list[str] = []

    async def crashing(**kwargs: Any) -> Any:
        runs.append(kwargs["prompt"])
        raise RuntimeError("worker lost")

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", crashing)
    with pytest.raises(RuntimeError):
        await _judge(runner, workspace_id, state)

    resumed = await _persisted(factory, workspace_id)
    assert resumed.threads_addressed_ids[_ack_key()] == UNJUDGED
    resumed.mark_addressed(_disposition_ack_pending_key(_SILENT), "0")
    await _judge(runner, workspace_id, resumed)
    assert len(runs) == 1


async def test_repeated_re_read_faults_end_in_a_failed_check_and_the_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state, gh, workspace_id, runner = await _flagged(factory, tmp_path)
    gh.refetch_error = GitHubClientError(
        operation="fetch_review_thread_audit", returncode=1, stderr="Review thread not found"
    )

    for _ in range(3):  # each poll: the step's re-read fails, then the gate re-audits
        await _judge(runner, workspace_id, state)
        await _gate(runner, workspace_id, state)

    [event] = await _ack_events(factory, workspace_id)
    assert (event["outcome"], event["reason_code"]) == ("check_failed", _CHECK_FAILED)
    assert event["evidence"] == {"thread_id": _SILENT, "detail": "refetch_failed"}
    assert state.threads_addressed_ids[_ack_key()] == UNJUDGED
    await _execute(runner, workspace_id, NotifyHuman(), state)
    assert gh.unresolved == [_SILENT]
    assert runner._deps.adapter.calls == []
