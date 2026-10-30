"""Automatic reopen of disposition-unverified review threads (#1138).

The #1018 gate refuses READY_TO_MERGE / merge on a thread AWF's identity
resolved without a live disposition. Two causes hit threads AWF did handle: a
review bot replying in the thread after AWF's marker, and a moved-head
re-adoption retiring the marker. Their only remedy used to be a human reopening
each thread. The runner now reopens them itself (at most twice per thread over
the PR's life) and re-polls; the next poll that lists the thread open re-triages
it through the normal fix cycle, which posts a fresh disposition before
resolving it. Over the cap, on a forge with no reopen API, or when the reopen
fails, today's ``NotifyHuman`` blocker applies — never a workspace failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import AsyncCommandRunner, FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import AuditedReviewThread, ReviewThreadAudit
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import (
    _disposition_auto_reopen_key,
    _disposition_retired_key,
    _disposition_unverified_key,
)
from awf.runtime.pr_monitor import (
    AddressComments,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    ReviewThread,
    ReviewThreadComment,
    decide,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import VerdictResult
from awf.runtime.pr_monitor_runner.lifecycle import _load_state
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    read_item_disposition,
    record_item_disposition,
    retire_reopened_dispositions,
    sync_item_disposition,
)
from awf.runtime.pr_monitor_runner.thread_disposition_reopen import MAX_AUTO_REOPENS_PER_THREAD
from awf.runtime.pr_monitor_runner.thread_disposition_reply import reply_before_resolve
from awf.service.pr_monitor_adoption_seed import seedable_monitor_state
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._thread_disposition_fixtures import settled_baseline_state
from tests.unit.runtime.test_pr_monitor_thread_disposition_gate import (
    _AUDIT,
    _FRESH,
    _SILENT,
    _VIEWER,
    _attention,
    _audit_events,
    _execute,
    _gate,
    _green_status,
    _marker,
    _release_runner,
    _thread,
)

pytestmark = pytest.mark.unit

_UNVERIFIED = "REVIEW_THREAD_DISPOSITION_UNVERIFIED"
_REOPENED = "REVIEW_THREAD_DISPOSITION_AUTO_REOPENED"
_EXHAUSTED = "REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_EXHAUSTED"
_UNSUPPORTED = "REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_UNSUPPORTED"
_FAILED = "REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_FAILED"
_BOT_ACK = "Agreed, this is a false positive."
_FEEDBACK = "P2: guard the None payload"
_REASON = "the caller already rejects None payloads"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class ReopenForge(DefaultMergeMethodGitHubClient):
    """Serves a canned audit, records PR comments, and reopens threads in it.

    ``unresolve`` is the forge's answer: True reopens the thread (the next audit
    lists it open), False means the forge has no reopen API, an error is a fault.
    """

    def __init__(
        self,
        runner: AsyncCommandRunner,
        *,
        audit: ReviewThreadAudit,
        unresolve: bool | ForgeClientError = True,
    ) -> None:
        super().__init__(runner)
        self.audit = audit
        self.unresolve = unresolve
        self.unresolved: list[str] = []
        self.comments: list[str] = []

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int, retry: bool = True, thread_id: str | None = None
    ) -> ReviewThreadAudit:
        del repo, pr_number, retry, thread_id
        return self.audit

    async def unresolve_thread(self, *, thread_id: str) -> bool:
        self.unresolved.append(thread_id)
        if isinstance(self.unresolve, ForgeClientError):
            raise self.unresolve
        if self.unresolve:
            self.audit = replace(
                self.audit,
                threads=tuple(
                    replace(t, is_resolved=False, resolved_by=None)
                    if t.thread_id == thread_id
                    else t
                    for t in self.audit.threads
                ),
            )
        return self.unresolve

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        del repo, pr_number
        self.comments.append(body)


def audit_of(*threads: AuditedReviewThread) -> ReviewThreadAudit:
    return ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=threads)


def bot_acked(tid: str = _SILENT) -> AuditedReviewThread:
    """Cause 1: AWF replied and resolved; the review bot then replied in the thread."""
    return replace(
        _thread(
            tid,
            bodies=(_marker("false_positive", tid),),
            superseded=1,
            external=(_FEEDBACK, _BOT_ACK),
        ),
        conversation_digest="d" * 64,
    )


def _listed(tid: str = _SILENT) -> ReviewThread:
    """The reopened thread as the next poll's status lists it."""
    return ReviewThread(
        thread_id=tid,
        path="widgets/core.py",
        line=12,
        body_excerpt=_FEEDBACK,
        author="review-bot",
        comments=(
            ReviewThreadComment(comment_id="1", body=_FEEDBACK),
            ReviewThreadComment(comment_id="3", body=_BOT_ACK),
        ),
    )


def handled_state(tid: str = _SILENT) -> MonitorState:
    """AWF's FALSE POSITIVE on ``tid``: reply posted, resolve landed, audit bound it."""
    state = settled_baseline_state()
    state.mark_addressed(tid, "false_positive")
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=tid,
            disposition_class="false_positive",
            verdict="false_positive",
            reason=_REASON,
            reply_comment_id="reply-1",
            resolved=True,
            conversation_digest="c" * 64,
        ),
    )
    return state


async def reopen_events(
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
            if row.payload.get("action") == "review_thread_disposition_auto_reopen"
        ]


async def test_cause_1_bot_ack_after_the_reply_reopens_and_re_triages(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = handled_state()
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(bot_acked()))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    with structlog.testing.capture_logs() as captured:
        terminal = await _execute(runner, workspace_id, NotifyHuman(), state)

    # Reopened instead of the blocker: no PR comment, no attention, one poll wait.
    assert terminal is False
    assert gh.comments == []
    since, reason, status = await _attention(factory, workspace_id)
    assert (since, reason, status) == (None, None, WorkspaceStatus.monitoring_pr.value)
    assert gh.unresolved == [_SILENT]
    assert runner._deps.sleep.calls == [60]
    assert state.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == "1"
    # The gate's flag is kept, so the poll that lists the thread open re-triages it.
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)] == _UNVERIFIED
    events = await reopen_events(factory, workspace_id)
    assert [(e["outcome"], e["reason_code"]) for e in events] == [("reopened", _REOPENED)]
    assert events[0]["evidence"]["thread_ids"] == [_SILENT]
    assert events[0]["evidence"]["reopen_counts"] == {_SILENT: 1}
    logs = [r for r in captured if r["event"] == "monitor.review_thread_disposition_auto_reopened"]
    assert [(r["reason_code"], r["thread_ids"]) for r in logs] == [(_REOPENED, [_SILENT])]

    # Next poll: the status lists the thread open; the existing re-triage path runs.
    reopened = replace(_green_status(), unresolved_inline_threads=(_listed(),))
    retire_reopened_dispositions(state, reopened, workspace_id=workspace_id)
    action = decide(reopened, state, MonitorConfig(auto_merge=False))
    assert isinstance(action, AddressComments)
    assert [t.thread_id for t in action.threads] == [_SILENT]
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)] == "reopened"
    # No record left to reuse (the bot's reply retired it): the re-triage replies afresh.
    assert read_item_disposition(state, _SILENT) is None

    # The re-triage's verdict: the fix cycle replies afresh before resolving.
    state.mark_addressed(_SILENT, "false_positive")
    sync_item_disposition(
        state, _SILENT, VerdictResult(verdict="false_positive", reason=_REASON), thread=_listed()
    )
    replied = await reply_before_resolve(
        SimpleNamespace(_monitor_owner_id=None, _deps=runner._deps),
        workspace_id=workspace_id,
        pr_number=42,
        thread_id=_SILENT,
        state=state,
        action="resolve_thread",
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
    )
    assert replied is not None and replied.reply_comment_id == "reply-1"
    assert [tid for tid, _ in gh.thread_replies] == [_SILENT]
    gh.audit = audit_of(
        replace(
            bot_acked(),
            viewer_comment_bodies=(_marker("false_positive", _SILENT), gh.thread_replies[0][1]),
            conversation_digest="e" * 64,
            conversation_digest_before_reply="d" * 64,
        )
    )

    certified = await _gate(runner, workspace_id, state)

    assert certified.blocker is None and certified.count_rows is not None
    assert dict(certified.count_rows)["false positive"] == 1
    assert _disposition_unverified_key(_SILENT) not in state.threads_addressed_ids


async def test_cause_2_moved_head_re_adoption_reopens_and_re_triages(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # The predecessor replied FALSE POSITIVE and resolved; its head then moved, so
    # the re-adoption crosses the record only as a retired-reply tombstone.
    predecessor = handled_state()
    adopted = MonitorState()
    for key, value in seedable_monitor_state(
        predecessor.threads_addressed_ids, head_continuity=False
    ).items():
        adopted.mark_addressed(key, value)
    assert _disposition_retired_key(_SILENT, "c" * 64) in adopted.threads_addressed_ids
    assert read_item_disposition(adopted, _SILENT) is None
    unchanged = replace(
        _thread(_SILENT, bodies=(_marker("false_positive", _SILENT),), external=(_FEEDBACK,)),
        conversation_digest="f" * 64,
        conversation_digest_before_reply="c" * 64,
    )
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(unchanged))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    terminal = await _execute(runner, workspace_id, NotifyHuman(), adopted)

    assert terminal is False
    assert gh.comments == []
    assert gh.unresolved == [_SILENT]
    assert adopted.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == "1"
    listed = replace(_listed(), comments=_listed().comments[:1])
    reopened = replace(_green_status(), unresolved_inline_threads=(listed,))
    retire_reopened_dispositions(adopted, reopened, workspace_id=workspace_id)
    action = decide(reopened, adopted, MonitorConfig(auto_merge=False))
    assert isinstance(action, AddressComments)
    assert [t.thread_id for t in action.threads] == [_SILENT]
    assert _SILENT not in adopted.threads_addressed_ids  # verdict cleared for re-triage


async def test_the_third_unverified_episode_posts_todays_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    assert MAX_AUTO_REOPENS_PER_THREAD == 2
    state = handled_state()
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(bot_acked()))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    for episode in (1, 2):
        # The bot acknowledges again after each fresh re-resolve.
        gh.audit = audit_of(bot_acked())
        assert await _execute(runner, workspace_id, NotifyHuman(), state) is False
        assert state.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == str(episode)
    assert gh.comments == []
    gh.audit = audit_of(bot_acked())

    terminal = await _execute(runner, workspace_id, NotifyHuman(), state)

    assert terminal is False
    assert gh.unresolved == [_SILENT, _SILENT]  # no third reopen
    assert state.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == "2"
    assert len(gh.comments) == 1
    assert _UNVERIFIED in gh.comments[0] and _EXHAUSTED in gh.comments[0]
    assert _SILENT in gh.comments[0] and "reopen it" in gh.comments[0]
    since, reason, status = await _attention(factory, workspace_id)
    assert since is not None and reason is not None and reason.startswith(f"{_UNVERIFIED}:")
    assert _EXHAUSTED in reason
    assert status == WorkspaceStatus.monitoring_pr.value
    assert len(await reopen_events(factory, workspace_id)) == 2

    # The count crosses a re-adoption (moved head or not): the successor blocks too.
    successor = MonitorState()
    for key, value in seedable_monitor_state(
        state.threads_addressed_ids, head_continuity=False
    ).items():
        successor.mark_addressed(key, value)
    assert successor.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == "2"
    next_gh = ReopenForge(FakeCommandRunner(), audit=audit_of(bot_acked()))
    next_workspace, next_runner = await _release_runner(factory, tmp_path, next_gh)
    assert await _execute(next_runner, next_workspace, NotifyHuman(), successor) is False
    assert next_gh.unresolved == []
    assert len(next_gh.comments) == 1 and _EXHAUSTED in next_gh.comments[0]


@pytest.mark.parametrize("count", ["x", "0", "-1", ""])
async def test_a_malformed_reopen_count_counts_as_exhausted(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    count: str,
) -> None:
    state = handled_state()
    state.mark_addressed(_disposition_auto_reopen_key(_SILENT), count)
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(bot_acked()))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert gh.unresolved == []
    assert len(gh.comments) == 1 and _EXHAUSTED in gh.comments[0]


async def test_a_partial_reopen_blocks_only_on_the_thread_left_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = handled_state()
    state.mark_addressed(_disposition_auto_reopen_key(_SILENT), "2")
    gh = ReopenForge(
        FakeCommandRunner(), audit=audit_of(bot_acked(), _thread(_FRESH, external=(_FEEDBACK,)))
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert gh.unresolved == [_FRESH]
    assert state.threads_addressed_ids[_disposition_auto_reopen_key(_FRESH)] == "1"
    assert len(gh.comments) == 1
    _, reason, _ = await _attention(factory, workspace_id)
    assert reason is not None and reason.startswith(f"{_UNVERIFIED}: 1 review thread(s)")
    assert _SILENT in reason and _FRESH not in reason and _EXHAUSTED in reason
    events = await reopen_events(factory, workspace_id)
    assert [e["evidence"]["thread_ids"] for e in events] == [[_FRESH]]


async def test_a_forge_without_a_reopen_api_keeps_todays_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = handled_state()
    gh = ReopenForge(
        FakeCommandRunner(),
        audit=audit_of(bot_acked(), _thread(_FRESH, external=(_FEEDBACK,))),
        unresolve=False,
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    with structlog.testing.capture_logs() as captured:
        terminal = await _execute(runner, workspace_id, NotifyHuman(), state)

    assert terminal is False
    assert gh.unresolved == [_SILENT]  # one probe: no further calls once unsupported
    assert len(gh.comments) == 1
    assert _UNVERIFIED in gh.comments[0] and _UNSUPPORTED in gh.comments[0]
    assert _SILENT in gh.comments[0] and _FRESH in gh.comments[0]
    assert _disposition_auto_reopen_key(_SILENT) not in state.threads_addressed_ids
    _, _, status = await _attention(factory, workspace_id)
    assert status == WorkspaceStatus.monitoring_pr.value
    assert await reopen_events(factory, workspace_id) == []
    assert len([r for r in captured if r.get("reason_code") == _UNSUPPORTED]) == 1


async def test_a_failed_reopen_keeps_todays_blocker_and_retries_next_poll(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = handled_state()
    fault = GitHubClientError(
        operation="gh api graphql", returncode=1, stderr="HTTP 403 token=ghp_secretvalue123"
    )
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(bot_acked()), unresolve=fault)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    with structlog.testing.capture_logs() as captured:
        terminal = await _execute(runner, workspace_id, NotifyHuman(), state)

    assert terminal is False
    assert len(gh.comments) == 1
    assert _UNVERIFIED in gh.comments[0] and _FAILED in gh.comments[0]
    assert "ghp_secretvalue123" not in gh.comments[0]
    assert _disposition_auto_reopen_key(_SILENT) not in state.threads_addressed_ids
    _, _, status = await _attention(factory, workspace_id)
    assert status == WorkspaceStatus.monitoring_pr.value
    failed = [r for r in captured if r.get("reason_code") == _FAILED]
    assert len(failed) == 1
    assert failed[0]["forge_reason_code"] == fault.reason_code
    assert failed[0]["thread_id"] == _SILENT
    assert "ghp_secretvalue123" not in str(failed[0])
    blocked_events = await _audit_events(factory, workspace_id)
    assert [e["outcome"] for e in blocked_events] == ["blocked"]  # the gate's own, unchanged

    # The forge recovers: the next poll's episode reopens the thread.
    gh.unresolve = True
    assert await _execute(runner, workspace_id, NotifyHuman(), state) is False
    assert gh.unresolved == [_SILENT, _SILENT]
    assert state.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == "1"
    assert len(gh.comments) == 1


async def test_the_reopen_count_survives_a_state_reload(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # The loop persists ``state`` after every handler return; the count is a
    # plain reserved key, so a reload (a restart, or the next claim) keeps it.
    state = handled_state()
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(bot_acked()))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    await _execute(runner, workspace_id, NotifyHuman(), state)

    await runner._persist_state(workspace_id, state)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    reloaded = _load_state(None, ws)

    assert reloaded.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == "1"
    assert reloaded.threads_addressed_ids[_disposition_unverified_key(_SILENT)] == _UNVERIFIED
