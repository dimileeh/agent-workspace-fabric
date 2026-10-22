"""Reply-before-resolve in the fix cycle (#1018).

Every fix-cycle resolve (FIXED, FALSE POSITIVE, captured DEFER) is preceded by
one in-thread disposition reply. A missing reason / commit, or a reply the forge
rejects, leaves the thread open and parks it ``needs_human`` with a reason code;
the workspace keeps monitoring and the pushed commits stay pushed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewThread,
    decide,
)
from awf.runtime.pr_monitor_runner import fix_cycle as fix_cycle_module
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    attach_issue_url,
    read_item_disposition,
    record_item_disposition,
    sync_item_disposition,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._thread_disposition_fixtures import (
    OrderedForge,
    seed_resolvable_disposition,
)

pytestmark = pytest.mark.unit

_REPO = RepoRef(owner="acme", name="widgets")
_START = "a" * 40
_PUSHED = "b" * 40
_FIX = "c" * 40
_ISSUE = "https://github.com/acme/widgets/issues/7"
_MARKER = "<!-- AWF:thread-disposition:v1 class={cls} thread={tid} -->"
_AUDIT = "workspace.audit.comment_resolution"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _thread(thread_id: str, body: str = "please guard None payloads") -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path="widgets/core.py",
        line=12,
        body_excerpt=body,
        author="review-bot",
    )


def _status(head_sha: str, threads: tuple[ReviewThread, ...] = ()) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


AddressFn = Callable[[MonitorState, ReviewThread], None]


async def _run_cycle(
    *,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gh: OrderedForge,
    verdict: str,
    on_address: AddressFn | None,
    thread: ReviewThread,
    pushed: bool = True,
    state: MonitorState | None = None,
) -> tuple[str, MonitorState, Any]:
    workspace_id = await seed_monitoring_workspace(factory)
    worktrees_root = tmp_path / "worktrees"
    (worktrees_root / workspace_id).mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=cast(FakeCommandRunner, gh._runner),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
        gh=gh,
    )
    state = state if state is not None else MonitorState()

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (_START, None)

    async def _none(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return _PUSHED

    async def _address_thread(**kwargs: object) -> str:
        addressed = cast(ReviewThread, kwargs["thread"])
        if on_address is not None:
            on_address(state, addressed)
        return verdict

    async def _settle(**_kwargs: object) -> PRStatus:
        return _status(_START)

    async def _push(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=pushed, failed=False, returncode=0)

    async def _capture(_self: object, **kwargs: object) -> bool:
        captured = cast(ReviewThread, kwargs["thread"])
        attach_issue_url(state, captured.thread_id, _ISSUE)
        return True

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(gh, "fetch_pr_status", _settle)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push)
    monkeypatch.setattr(fix_cycle_module, "_capture_deferred_review_thread", _capture)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        pr_head_sha=_START,
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )
    return workspace_id, state, result


async def _resolution_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[dict[str, Any]]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == _AUDIT,
            )
            .order_by(WorkspaceEvent.event_order)
        )
        return [dict(row.payload) for row in rows.scalars().all()]


async def _workspace_status(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> str:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    return ws.status


@pytest.mark.parametrize(
    ("verdict", "cls", "expected_text"),
    [
        ("fix_committed", "fixed", _FIX),
        ("false_positive", "false_positive", "the reviewed code already handles this case"),
        ("defer", "deferred", _ISSUE),
    ],
)
async def test_fix_cycle_posts_reply_before_resolve_for_each_resolvable_verdict(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
    cls: str,
    expected_text: str,
) -> None:
    gh = OrderedForge(FakeCommandRunner())
    tid = f"PRRT_test_{cls}"

    def _seed(state: MonitorState, thread: ReviewThread) -> None:
        if verdict == "defer":
            seed_resolvable_disposition(state, thread.thread_id, verdict, commits=(_FIX,))
            disposition = read_item_disposition(state, thread.thread_id)
            assert disposition is not None
            # The issue arrives from the (monkeypatched) capture, as in production.
            record_item_disposition(
                state,
                ThreadDisposition(
                    item_id=thread.thread_id,
                    disposition_class="deferred",
                    verdict="defer",
                    reason=disposition.reason,
                ),
            )
        else:
            seed_resolvable_disposition(state, thread.thread_id, verdict, commits=(_FIX,))

    workspace_id, state, result = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict=verdict,
        on_address=_seed,
        thread=_thread(tid),
    )

    assert result.failed is False
    assert [event[0] for event in gh.events] == ["reply", "resolve"]
    assert gh.events[0][1] == tid and gh.events[1] == ("resolve", tid)
    body = gh.events[0][2]
    assert body.splitlines()[0] == _MARKER.format(cls=cls, tid=tid)
    assert expected_text in body
    assert "@" not in body
    recorded = read_item_disposition(state, tid)
    assert recorded is not None and recorded.reply_comment_id == "reply-1"
    succeeded = [
        e
        for e in await _resolution_events(factory, workspace_id)
        if e["action"] == "resolve_thread"
    ]
    assert [e["outcome"] for e in succeeded] == ["succeeded"]
    dispositions = succeeded[0]["evidence"]["dispositions"]
    assert dispositions[0]["class"] == cls
    assert dispositions[0]["reply_comment_id"] == "reply-1"


async def test_fix_cycle_reply_failure_parks_without_resolve_and_keeps_pushed_commits(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = OrderedForge(
        FakeCommandRunner(),
        reply_error=GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502"),
    )
    tid = "PRRT_test_reply_fail"
    thread = _thread(tid)

    workspace_id, state, result = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="fix_committed",
        on_address=lambda s, t: seed_resolvable_disposition(s, t.thread_id, "fix_committed"),
        thread=thread,
    )

    assert not [event for event in gh.events if event[0] == "resolve"]
    assert state.threads_addressed_ids[tid] == "needs_human"
    reason = state.threads_addressed_ids["__needs_human_reason__:" + tid]
    assert reason.startswith("THREAD_DISPOSITION_REPLY_FAILED:")
    assert result.failed is False
    assert result.pushed is True
    assert state.last_push_sha == _PUSHED
    assert await _workspace_status(factory, workspace_id) == WorkspaceStatus.monitoring_pr.value
    action = decide(_status(_PUSHED, (thread,)), state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    parked = [
        e
        for e in await _resolution_events(factory, workspace_id)
        if e["action"] == "resolve_thread"
    ]
    assert [(e["outcome"], e["reason_code"]) for e in parked] == [
        ("needs_human", "THREAD_DISPOSITION_REPLY_FAILED")
    ]
    assert parked[0]["evidence"]["thread_ids"] == [tid]
    assert parked[0]["evidence"]["forge_reason_code"] == "GITHUB_API_ERROR"
    assert parked[0]["evidence"]["disposition"]["class"] == "fixed"


@pytest.mark.parametrize(
    "resolve_stderr",
    ["HTTP 502", "HTTP 404: Not Found"],
    ids=["transient", "deterministic"],
)
async def test_active_thread_resolve_retry_reuses_recorded_reply_without_reposting(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolve_stderr: str,
) -> None:
    gh = OrderedForge(
        FakeCommandRunner(),
        resolve_error=GitHubClientError(
            operation="gh api graphql", returncode=1, stderr=resolve_stderr
        ),
    )
    tid = "PRRT_test_active_retry"
    thread = _thread(tid)

    def _verdict(reason: str) -> AddressFn:
        # What ``comments._address_thread`` records at verdict time.
        def _record(state: MonitorState, addressed: ReviewThread) -> None:
            sync_item_disposition(
                state, addressed.thread_id, VerdictResult(verdict="false_positive", reason=reason)
            )

        return _record

    _, state, _ = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=_verdict("the reviewed code already handles this case"),
        thread=thread,
    )
    assert [event[0] for event in gh.events] == ["reply", "resolve_failed"]
    # The active thread still re-enters AddressComments, but its posted reply is kept.
    assert tid not in state.threads_addressed_ids
    recorded = read_item_disposition(state, tid)
    assert recorded is not None and recorded.reply_comment_id == "reply-1"

    gh.resolve_error = None
    await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=_verdict("re-run wording of the same false positive"),
        thread=thread,
        state=state,
    )

    assert [event[0] for event in gh.events] == ["reply", "resolve_failed", "resolve"]
    assert len(gh.replies()) == 1
    recorded = read_item_disposition(state, tid)
    assert recorded is not None and recorded.reply_comment_id == "reply-1"


async def test_fix_cycle_false_positive_without_reason_parks(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_fp_bare"

    workspace_id, state, result = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=None,  # the legacy / in-flight shape: no recorded disposition
        thread=_thread(tid),
        pushed=False,
    )

    assert gh.events == []
    assert result.failed is False
    assert state.threads_addressed_ids[tid] == "needs_human"
    assert state.threads_addressed_ids["__needs_human_reason__:" + tid].startswith(
        "THREAD_DISPOSITION_REASON_MISSING:"
    )
    parked = [
        e
        for e in await _resolution_events(factory, workspace_id)
        if e["action"] == "resolve_thread"
    ]
    assert [(e["outcome"], e["reason_code"]) for e in parked] == [
        ("needs_human", "THREAD_DISPOSITION_REASON_MISSING")
    ]


async def test_fix_cycle_fixed_without_commit_parks_evidence_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_fixed_bare"

    def _pending(state: MonitorState, thread: ReviewThread) -> None:
        record_item_disposition(
            state,
            ThreadDisposition(
                item_id=thread.thread_id, disposition_class="fixed", verdict="fix_committed"
            ),
        )

    _, state, result = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="fix_committed",
        on_address=_pending,
        thread=_thread(tid),
    )

    assert gh.events == []
    assert result.pushed is True
    assert state.threads_addressed_ids[tid] == "needs_human"
    assert state.threads_addressed_ids["__needs_human_reason__:" + tid].startswith(
        "THREAD_DISPOSITION_EVIDENCE_MISSING:"
    )


async def test_fix_cycle_defer_reply_failure_does_not_resolve(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a DEFER whose explanation could not be posted is never resolved.

    The old capture swallowed a failed top-level comment and resolved anyway.
    """
    gh = OrderedForge(
        FakeCommandRunner(),
        reply_error=GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 403"),
    )
    tid = "PRRT_test_defer_fail"
    filed_marker = f"__deferred_issue_filed__:{tid}:hash"

    def _defer(state: MonitorState, thread: ReviewThread) -> None:
        state.mark_addressed(f"__defer_reason__:{thread.thread_id}", "track the follow-up")
        state.mark_addressed(filed_marker, _ISSUE)

    _, state, result = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="defer",
        on_address=_defer,
        thread=_thread(tid),
        pushed=False,
    )

    assert gh.events == [("reply_failed", tid)]
    assert result.failed is False
    assert state.threads_addressed_ids[tid] == "needs_human"
    assert state.threads_addressed_ids["__needs_human_reason__:" + tid].startswith(
        "THREAD_DISPOSITION_REPLY_FAILED:"
    )
    # The filed-issue marker survives, so a retry never files a duplicate issue.
    assert state.threads_addressed_ids[filed_marker] == _ISSUE


async def test_silently_resolved_p1_false_positive_now_replies_first(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #1018: a P1 finding dismissed with no code change.

    Previously the thread was resolved with nothing pushed and no reply. Now the
    reply carrying the agent's reason precedes the resolve, and a failed reply
    keeps the thread open.
    """
    tid = "PRRT_test_p1"
    reason = "None payloads are rejected by validate_payload before the worker runs"
    thread = _thread(tid, body="**P1** Badge: worker crashes when payload is None")

    def _fp(state: MonitorState, addressed: ReviewThread) -> None:
        seed_resolvable_disposition(state, addressed.thread_id, "false_positive", reason=reason)

    gh = OrderedForge(FakeCommandRunner())
    _, _, result = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=_fp,
        thread=thread,
        pushed=False,
    )
    assert result.pushed is False
    assert [event[0] for event in gh.events] == ["reply", "resolve"]
    assert reason in gh.events[0][2]

    failing = OrderedForge(
        FakeCommandRunner(),
        reply_error=GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502"),
    )
    _, state, _ = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path / "second",
        monkeypatch=monkeypatch,
        gh=failing,
        verdict="false_positive",
        on_address=_fp,
        thread=thread,
        pushed=False,
    )
    assert failing.events == [("reply_failed", tid)]
    assert state.threads_addressed_ids[tid] == "needs_human"


async def test_fix_cycle_reply_reason_never_mentions(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_mention"

    _, _, _ = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=lambda s, t: seed_resolvable_disposition(
            s, t.thread_id, "false_positive", reason="@alice and @org/team already fixed this"
        ),
        thread=_thread(tid),
        pushed=False,
    )

    replies = gh.replies()
    assert len(replies) == 1
    assert "@" not in replies[0][1]
    assert "alice" in replies[0][1]
