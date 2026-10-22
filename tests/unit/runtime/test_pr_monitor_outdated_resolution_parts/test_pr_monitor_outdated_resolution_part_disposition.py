"""Outdated-thread hygiene posts its disposition reply before resolving (#1018).

Resolve path 2 resolved outdated threads on any recorded verdict — including a
``fix_committed`` seeded only because a branch commit message matched, a verdict
this monitor never produced. Every such resolve now follows one in-thread reply:
the fix commit, the agent's reason, the reason plus issue link, or the matched
branch commit for seeded threads. A reply that cannot be posted parks the thread.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import BITBUCKET_API_ERROR, BitbucketClientError
from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import GitHubClientError
from awf.db.repositories import WorkspaceRepository
from awf.runtime.monitor_state_keys import _outdated_resolve_requeued_key
from awf.runtime.pr_monitor import MonitorState, _review_thread_body_hash
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import VerdictResult
from awf.runtime.pr_monitor_runner.thread_disposition import (
    read_item_disposition,
    sync_item_disposition,
)
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
from tests.unit.runtime.test_pr_monitor_outdated_resolution_parts._helpers import (
    _call_resolve,
    _outdated_thread,
    _outdated_thread_with_comment,
    _outdated_thread_with_distinct_comment,
    _outdated_thread_with_reply,
    _resolution_events,
    _status_with_outdated,
)

pytestmark = pytest.mark.unit

_MARKER = "<!-- AWF:thread-disposition:v1 class={cls} thread={tid} -->"
_ISSUE = "https://github.com/acme/widgets/issues/7"
_TRANSIENT = GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502")
_PERMANENT = BitbucketClientError(
    operation="bitbucket resolve_thread",
    status=403,
    body="thread resolution is not permitted for this token",
    reason_code=BITBUCKET_API_ERROR,
)


def _mark(state: MonitorState, tid: str, verdict: str, *, path: str = "src/anchor.py") -> None:
    from awf.runtime.pr_monitor import _mark_review_thread_addressed

    _mark_review_thread_addressed(state, _outdated_thread(tid, path=path), verdict)


async def _runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    gh: OrderedForge,
) -> tuple[str, Any]:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=gh._runner,  # type: ignore[arg-type]
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    return workspace_id, runner


async def _events(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> list[Any]:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
    assert ws is not None
    return _resolution_events(ws)


async def test_outdated_fixed_thread_replies_then_resolves(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = OrderedForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    tid = "PRRT_test_outdated_fixed"
    _mark(state, tid, "fix_committed")
    seed_resolvable_disposition(state, tid, "fix_committed", commits=("e" * 40,))

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(_outdated_thread(tid)),
        state=state,
    )

    assert [event[0] for event in gh.events] == ["reply", "resolve"]
    body = gh.events[0][2]
    assert body.splitlines()[0] == _MARKER.format(cls="fixed", tid=tid)
    assert "e" * 40 in body
    succeeded = [
        e for e in await _events(factory, workspace_id) if e.payload["outcome"] == "succeeded"
    ]
    assert succeeded[0].payload["evidence"]["dispositions"][0]["class"] == "fixed"
    assert succeeded[0].payload["evidence"]["dispositions"][0]["reply_comment_id"] == "reply-1"


@pytest.mark.parametrize(
    ("resolve_error", "outcome"),
    [(_TRANSIENT, "requeued"), (_PERMANENT, "needs_human")],
)
async def test_failed_outdated_resolve_event_records_posted_disposition(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    resolve_error: ForgeClientError,
    outcome: str,
) -> None:
    # PRRT_kwDOSJAM6s6vUJzD: the reply landed but the resolve failed; the audit
    # event must still show which durable reply preceded the failed resolution.
    gh = OrderedForge(FakeCommandRunner(), resolve_error=resolve_error)
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    tid = "PRRT_test_outdated_resolve_failed"
    _mark(state, tid, "fix_committed")
    seed_resolvable_disposition(state, tid, "fix_committed", commits=("e" * 40,))

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(_outdated_thread(tid)),
        state=state,
        wait_on_transient=False,
    )

    assert [event[0] for event in gh.events] == ["reply", "resolve_failed"]
    failed = [e for e in await _events(factory, workspace_id) if e.payload["outcome"] == outcome]
    dispositions = failed[0].payload["evidence"]["dispositions"]
    assert [(d["class"], d["reply_comment_id"]) for d in dispositions] == [("fixed", "reply-1")]


async def test_branch_evidence_seed_replies_citing_matched_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    gh = OrderedForge(cmd)
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    tid = "PRRT_test_seeded"
    matched = "d" * 40
    cmd.respond_when(
        lambda argv: "log" in argv and "--grep" in argv,
        stdout=f"{matched} 2026-10-01T10:00:00+00:00\n",
    )

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(_outdated_thread(tid)),
        state=state,
    )

    assert any("--format=%H %aI" in call.args for call in cmd.calls)
    disposition = read_item_disposition(state, tid)
    assert disposition is not None
    assert disposition.disposition_class == "fixed_branch_evidence"
    assert disposition.commits == (matched,)
    assert disposition.source == "branch_evidence"
    assert [event[0] for event in gh.events] == ["reply", "resolve"]
    body = gh.events[0][2]
    assert body.splitlines()[0] == _MARKER.format(cls="fixed_branch_evidence", tid=tid)
    assert matched in body


async def test_branch_evidence_reply_is_superseded_by_feedback_after_failed_resolve(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # PRRT_kwDOSJAM6s6vRfXm: the seeded reply lands, the resolve fails, then a
    # reviewer replies. The re-triaged verdict must not reuse the stale
    # branch-evidence reply, so the seed records the conversation it answers.
    cmd = FakeCommandRunner()
    gh = OrderedForge(cmd, resolve_error=_TRANSIENT)
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    tid = "PRRT_test_seeded_then_feedback"
    cmd.respond_when(
        lambda argv: "log" in argv and "--grep" in argv,
        stdout=f"{'d' * 40} 2026-10-01T10:00:00+00:00\n",
    )
    thread = _outdated_thread(tid)

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
        wait_on_transient=False,
    )

    assert [event[0] for event in gh.events] == ["reply", "resolve_failed"]
    posted = read_item_disposition(state, tid)
    assert posted is not None
    assert posted.reply_comment_id == "reply-1"
    assert posted.thread_body_hash == _review_thread_body_hash(thread)

    followed_up = _outdated_thread_with_reply(
        tid,
        addressed_comment_id="c1",
        addressed_at=datetime(2026, 9, 30, tzinfo=UTC),
        reply_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    sync_item_disposition(
        state,
        tid,
        VerdictResult(verdict="false_positive", reason="the follow-up is out of scope"),
        thread=followed_up,
    )

    retriaged = read_item_disposition(state, tid)
    assert retriaged is not None
    assert (retriaged.disposition_class, retriaged.reply_comment_id) == ("false_positive", None)


async def test_branch_evidence_seed_without_sha_does_not_seed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    gh = OrderedForge(cmd)
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    tid = "PRRT_test_no_sha"
    cmd.respond_when(
        lambda argv: "log" in argv and "--grep" in argv,
        stdout="2026-10-01T10:00:00+00:00\n",
    )

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(_outdated_thread(tid)),
        state=state,
    )

    assert tid not in state.threads_addressed_ids
    assert gh.events == []


async def test_branch_evidence_post_fix_reply_still_parks_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    gh = OrderedForge(cmd)
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    tid = "PRRT_test_post_fix"
    cmd.respond_when(
        lambda argv: "log" in argv and "--grep" in argv,
        stdout=f"{'d' * 40} 2026-10-01T10:00:00+00:00\n",
    )
    thread = _outdated_thread_with_comment(tid, comment_at=datetime(2026, 10, 2, tzinfo=UTC))

    await _call_resolve(
        runner, workspace_id=workspace_id, status=_status_with_outdated(thread), state=state
    )

    assert state.threads_addressed_ids[tid] == "needs_human"
    assert read_item_disposition(state, tid) is None
    assert gh.events == []


async def test_outdated_reply_failure_parks_and_persists(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = OrderedForge(FakeCommandRunner(), reply_error=_TRANSIENT)
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    persisted: list[dict[str, str]] = []

    async def _persist(_workspace_id: str, state: MonitorState) -> None:
        persisted.append(dict(state.threads_addressed_ids))

    monkeypatch.setattr(runner, "_persist_state", _persist)
    state = MonitorState()
    tid = "PRRT_test_outdated_fail"
    _mark(state, tid, "false_positive")
    seed_resolvable_disposition(state, tid, "false_positive")
    state.threads_addressed_ids[_outdated_resolve_requeued_key(tid)] = "requeued"

    result = await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(_outdated_thread(tid)),
        state=state,
    )

    assert gh.events == [("reply_failed", tid)]
    assert state.threads_addressed_ids[tid] == "needs_human"
    assert state.threads_addressed_ids[f"__needs_human_reason__:{tid}"].startswith(
        "THREAD_DISPOSITION_REPLY_FAILED:"
    )
    assert _outdated_resolve_requeued_key(tid) not in state.threads_addressed_ids
    assert persisted and persisted[-1][tid] == "needs_human"
    assert [t.thread_id for t in result.outdated_unresolved_inline_threads] == [tid]
    parked = [
        e for e in await _events(factory, workspace_id) if e.payload["outcome"] == "needs_human"
    ]
    assert parked[0].reason_code == "THREAD_DISPOSITION_REPLY_FAILED"


async def test_outdated_resolve_retry_reuses_recorded_reply_without_reposting(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = OrderedForge(FakeCommandRunner(), resolve_error=_TRANSIENT)
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    persisted: list[dict[str, str]] = []

    async def _persist(_workspace_id: str, state: MonitorState) -> None:
        persisted.append(dict(state.threads_addressed_ids))

    monkeypatch.setattr(runner, "_persist_state", _persist)
    state = MonitorState()
    tid = "PRRT_test_retry"
    _mark(state, tid, "fix_committed")
    seed_resolvable_disposition(state, tid, "fix_committed")
    status = _status_with_outdated(_outdated_thread(tid))

    await _call_resolve(
        runner, workspace_id=workspace_id, status=status, state=state, wait_on_transient=False
    )
    assert [event[0] for event in gh.events] == ["reply", "resolve_failed"]
    assert state.threads_addressed_ids[_outdated_resolve_requeued_key(tid)] == "requeued"
    # The new reply id was persisted before the resolve attempt.
    assert persisted and "reply-1" in persisted[0][f"__awf_item_disposition__:{tid}"]

    gh.resolve_error = None
    await _call_resolve(
        runner, workspace_id=workspace_id, status=status, state=state, wait_on_transient=False
    )
    assert [event[0] for event in gh.events] == ["reply", "resolve_failed", "resolve"]
    assert len(gh.replies()) == 1
    assert len(persisted) == 1  # no new reply -> nothing extra to persist


async def test_legacy_captured_defer_without_disposition_replies_with_reason_and_issue(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = OrderedForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    tid = "PRRT_test_legacy_defer"
    thread = _outdated_thread(tid)
    _mark(state, tid, "defer")
    state.mark_addressed(f"__defer_reason__:{tid}", "track the cache warmup follow-up")
    state.mark_addressed(
        f"__deferred_issue_filed__:{tid}:{_review_thread_body_hash(thread)}", _ISSUE
    )

    await _call_resolve(
        runner, workspace_id=workspace_id, status=_status_with_outdated(thread), state=state
    )

    assert [event[0] for event in gh.events] == ["reply", "resolve"]
    body = gh.events[0][2]
    assert body.splitlines()[0] == _MARKER.format(cls="deferred", tid=tid)
    assert "track the cache warmup follow-up" in body
    assert _ISSUE in body


async def test_promoted_comment_keyed_verdict_copies_disposition(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    gh = OrderedForge(cmd)
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    tid = "PRRT_test_promoted"
    state.mark_addressed("4688598838", "false_positive")
    seed_resolvable_disposition(
        state, "4688598838", "false_positive", reason="the payload is validated upstream"
    )
    thread = _outdated_thread_with_distinct_comment(tid, comment_id="4688598838")

    await _call_resolve(
        runner, workspace_id=workspace_id, status=_status_with_outdated(thread), state=state
    )

    assert cmd.calls == []
    assert [event[0] for event in gh.events] == ["reply", "resolve"]
    body = gh.events[0][2]
    assert body.splitlines()[0] == _MARKER.format(cls="false_positive", tid=tid)
    assert "the payload is validated upstream" in body


async def test_promoted_reply_is_superseded_by_feedback_after_failed_resolve(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # PRRT_kwDOSJAM6s6vTf6l: the comment-keyed verdict recorded no thread body
    # hash, so the promoted copy binds the conversation it answers. Otherwise a
    # reviewer reply after a failed resolve reused the stale reply on re-triage.
    gh = OrderedForge(FakeCommandRunner(), resolve_error=_TRANSIENT)
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    tid = "PRRT_test_promoted_then_feedback"
    addressed_at = datetime(2026, 9, 30, tzinfo=UTC)
    state.mark_addressed("4688598838", "false_positive")
    seed_resolvable_disposition(
        state, "4688598838", "false_positive", reason="the payload is validated upstream"
    )
    thread = _outdated_thread_with_distinct_comment(
        tid, comment_id="4688598838", comment_at=addressed_at
    )

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
        wait_on_transient=False,
    )

    assert [event[0] for event in gh.events] == ["reply", "resolve_failed"]
    posted = read_item_disposition(state, tid)
    assert posted is not None
    assert posted.reply_comment_id == "reply-1"
    assert posted.thread_body_hash == _review_thread_body_hash(thread)

    followed_up = _outdated_thread_with_reply(
        tid,
        addressed_comment_id="4688598838",
        addressed_at=addressed_at,
        reply_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    sync_item_disposition(
        state,
        tid,
        VerdictResult(verdict="false_positive", reason="the follow-up is out of scope"),
        thread=followed_up,
    )

    retriaged = read_item_disposition(state, tid)
    assert retriaged is not None
    assert (retriaged.reason, retriaged.reply_comment_id) == (
        "the follow-up is out of scope",
        None,
    )


async def test_outdated_false_positive_without_reason_parks(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = OrderedForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    tid = "PRRT_test_outdated_bare_fp"
    _mark(state, tid, "false_positive")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(_outdated_thread(tid)),
        state=state,
    )

    assert gh.events == []
    assert state.threads_addressed_ids[tid] == "needs_human"
    assert state.threads_addressed_ids[f"__needs_human_reason__:{tid}"].startswith(
        "THREAD_DISPOSITION_REASON_MISSING:"
    )
