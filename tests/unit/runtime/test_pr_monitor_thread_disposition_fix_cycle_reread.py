"""Post-reply re-read in the fix cycle (#1018).

A fresh disposition reply is checked against the live thread before AWF resolves
it. Feedback added or edited after the listed snapshot, or unmarked feedback by
AWF's own identity before the reply, keeps the thread open (re-triaged, or parked
for a human) and retires the reply; a thread that cannot be re-read is parked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_thread_audit import AuditedReviewThread, ReviewThreadAudit
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _disposition_retired_key
from awf.runtime.pr_monitor import (
    AddressComments,
    MonitorConfig,
    NotifyHuman,
    ReviewThread,
    ReviewThreadComment,
    decide,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import VerdictResult
from awf.runtime.pr_monitor_runner.lifecycle import _load_state
from awf.runtime.pr_monitor_runner.thread_disposition import (
    read_item_disposition,
    retire_reopened_dispositions,
    sync_item_disposition,
)
from awf.runtime.pr_monitor_runner.thread_disposition_reply import reply_before_resolve
from tests.postgres import postgres_test_engine
from tests.unit.runtime._thread_disposition_fixtures import (
    OrderedForge,
    seed_resolvable_disposition,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_fix_cycle import (
    _MARKER,
    _PUSHED,
    _REPO,
    _resolution_events,
    _run_cycle,
    _status,
    _thread,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


_ASK = "please guard None payloads"


def _commented_thread(thread_id: str, *bodies: str) -> ReviewThread:
    """The thread as ``fetch_pr_status`` lists it: its outside comments, oldest first."""
    return replace(
        _thread(thread_id, bodies[0]),
        comments=tuple(
            ReviewThreadComment(comment_id=f"c{index}", body=body, author="review-bot")
            for index, body in enumerate(bodies, start=1)
        ),
    )


def _serve_live_thread(
    gh: OrderedForge,
    monkeypatch: pytest.MonkeyPatch,
    thread_id: str,
    bodies: tuple[str, ...],
    *,
    supported: bool = True,
    error: GitHubClientError | None = None,
    before_reply: str = "",
    viewer_bodies: tuple[str, ...] = (),
    viewer_ids: tuple[str, ...] = (),
    superseded: int = 0,
) -> list[int]:
    """Make the audit list ``thread_id`` with ``bodies``; return the PRs it audited."""
    audited: list[int] = []

    async def _audit(*, repo: RepoRef, pr_number: int, retry: bool = True) -> ReviewThreadAudit:
        del repo, retry
        audited.append(pr_number)
        if error is not None:
            raise error
        live = AuditedReviewThread(
            thread_id=thread_id,
            is_resolved=False,
            is_outdated=False,
            resolved_by=None,
            viewer_comment_bodies=viewer_bodies,
            viewer_comment_ids=viewer_ids,
            superseded_viewer_comments=superseded,
            conversation_digest_before_reply=before_reply,
            external_comment_bodies=bodies,
        )
        return ReviewThreadAudit(supported=supported, viewer_login="awf-test-bot", threads=(live,))

    monkeypatch.setattr(gh, "fetch_review_thread_audit", _audit)
    return audited


@pytest.mark.parametrize(
    "live",
    [(_ASK, "and cover empty payloads too"), ("please guard None and empty payloads",)],
    ids=["reply-added", "edited"],
)
async def test_fix_cycle_keeps_a_thread_open_when_feedback_moved_before_the_reply(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live: tuple[str, ...],
) -> None:
    # PRRT_kwDOSJAM6s6vaQrr: feedback added or edited after the listed snapshot but
    # before the reply lands sits before the reply, so once resolved the gate would
    # read the reply as its answer. The live thread is re-read after the reply: the
    # resolve is skipped and the reply retired (saved at once, even for a record
    # with no conversation snapshot), so the re-triage its changed body triggers
    # answers that feedback with a fresh reply instead of reusing the stale one.
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_feedback_moved"
    thread = _commented_thread(tid, _ASK)
    followed = "d" * 64
    audited = _serve_live_thread(
        gh,
        monkeypatch,
        tid,
        live,
        before_reply=followed,
        viewer_bodies=(_MARKER.format(cls="false_positive", tid=tid),),
        viewer_ids=("reply-1",),
    )

    workspace_id, state, result = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=lambda s, t: seed_resolvable_disposition(s, t.thread_id, "false_positive"),
        thread=thread,
        settle_threads=(thread,),
    )

    assert [event[0] for event in gh.events] == ["reply"]
    assert audited == [42]
    assert result.failed is False and result.pushed is True
    assert state.threads_addressed_ids[tid] == "false_positive"
    assert not [
        e
        for e in await _resolution_events(factory, workspace_id)
        if e["action"] == "resolve_thread"
    ]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    for kept_state in (state, _load_state(None, ws)):
        kept = read_item_disposition(kept_state, tid)
        assert kept is not None and kept.reply_comment_id is None
        assert kept.reason == "the reviewed code already handles this case"
        # The retired reply follows that feedback: its marker never certifies it
        # (PRRT_kwDOSJAM6s6vaq4v), even once the record is gone.
        assert _disposition_retired_key(tid, followed) in kept_state.threads_addressed_ids
    moved = _commented_thread(tid, *live)
    action = decide(_status(_PUSHED, (moved,)), state, MonitorConfig(auto_merge=True))
    assert isinstance(action, AddressComments)
    assert [t.thread_id for t in action.threads] == [tid]

    sync_item_disposition(
        state, tid, VerdictResult(verdict="false_positive", reason="still handled"), thread=moved
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(gh=gh, session_factory=factory))
    replied = await reply_before_resolve(
        runner,
        workspace_id=workspace_id,
        pr_number=42,
        thread_id=tid,
        state=state,
        action="resolve_thread",
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        thread=moved,
        repo=_REPO,
    )

    assert replied is not None and replied.reply_comment_id == "reply-2"
    assert "still handled" in gh.replies()[-1][1]
    assert audited == [42, 42]


@pytest.mark.parametrize("supported", [True, False], ids=["unchanged", "unsupported-forge"])
async def test_fix_cycle_resolves_when_the_live_thread_shows_no_new_feedback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    supported: bool,
) -> None:
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_feedback_unchanged"
    thread = _commented_thread(tid, _ASK)
    # An unsupported audit proves nothing, whatever it lists.
    bodies = (_ASK,) if supported else (_ASK, "unverifiable")
    audited = _serve_live_thread(gh, monkeypatch, tid, bodies, supported=supported)

    _, state, _ = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=lambda s, t: seed_resolvable_disposition(s, t.thread_id, "false_positive"),
        thread=thread,
        settle_threads=(thread,),
    )

    assert [event[0] for event in gh.events] == ["reply", "resolve"]
    assert audited == [42]
    recorded = read_item_disposition(state, tid)
    assert recorded is not None and (recorded.reply_comment_id, recorded.resolved) == (
        "reply-1",
        True,
    )


_OPERATOR_ASK = "also cover empty payloads"


def _own_comment_ids(names: tuple[str, ...]) -> tuple[str, ...]:
    """The audit's ids for AWF's identity's comments: ``reply`` is the one just posted."""
    return tuple(
        "reply-1" if name == "reply" else f"own-{index}" for index, name in enumerate(names)
    )


@pytest.mark.parametrize(
    ("viewer_bodies", "superseded"),
    [
        ((_OPERATOR_ASK, "reply"), 0),
        ((_OPERATOR_ASK, "reply"), 1),
        ((_OPERATOR_ASK, "parked-reply", "reply"), 2),
        ((_OPERATOR_ASK,), 0),
        (("earlier-reply", _OPERATOR_ASK), 0),
    ],
    ids=[
        "right-before-the-reply",
        "before-outside-feedback",
        "before-a-superseded-awf-reply",
        "reply-not-yet-in-the-re-read",
        "after-an-earlier-reply-in-a-stale-re-read",
    ],
)
async def test_fix_cycle_parks_on_unmarked_own_identity_feedback_before_the_reply(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    viewer_bodies: tuple[str, ...],
    superseded: int,
) -> None:
    # PRRT_kwDOSJAM6s6vbU2H: the operator, under AWF's own account, replies after
    # the status read but before AWF's reply lands. ``fetch_pr_status`` lists none
    # of that identity's comments, so the outside comments still match, yet the
    # gate would read AWF's reply after it as its answer. The moved check compares
    # the full conversation before the reply, that identity's unmarked comments
    # included, even one newer outside feedback (``superseded``) followed: the
    # triage read that feedback, never the operator's. An earlier AWF reply that
    # such feedback superseded bounds nothing (PRRT_kwDOSJAM6s6vbrwH): the one an
    # earlier park retired, say, before a reviewer reply re-triaged the thread.
    # A stale re-read that does not show the reply yet bounds nothing either:
    # every comment of that identity it shows is unread (PRRT_kwDOSJAM6s6vc8M9),
    # and an earlier AWF reply it does show is not the reply, so the comment after
    # it precedes the reply too (PRRT_kwDOSJAM6s6veO2F).
    # The thread is parked for a human instead of resolved, and the reply retired
    # (saved at once) so its marker never certifies that feedback.
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_own_identity_feedback"
    thread = _commented_thread(tid, _ASK)
    followed = "f" * 64
    marker = _MARKER.format(cls="false_positive", tid=tid)
    _serve_live_thread(
        gh,
        monkeypatch,
        tid,
        (_ASK,),
        before_reply=followed,
        viewer_bodies=tuple(marker if body.endswith("reply") else body for body in viewer_bodies),
        viewer_ids=_own_comment_ids(viewer_bodies),
        superseded=superseded,
    )

    workspace_id, state, result = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=lambda s, t: seed_resolvable_disposition(s, t.thread_id, "false_positive"),
        thread=thread,
        settle_threads=(thread,),
    )

    assert [event[0] for event in gh.events] == ["reply"]
    assert result.failed is False and result.pushed is True
    assert state.threads_addressed_ids[tid] == "needs_human"
    reason = state.threads_addressed_ids["__needs_human_reason__:" + tid]
    assert reason.startswith("THREAD_DISPOSITION_UNREAD_FEEDBACK:")
    parked = [
        e
        for e in await _resolution_events(factory, workspace_id)
        if e["action"] == "resolve_thread"
    ]
    assert [(e["outcome"], e["reason_code"]) for e in parked] == [
        ("needs_human", "THREAD_DISPOSITION_UNREAD_FEEDBACK")
    ]
    assert parked[0]["evidence"]["disposition"]["reply_comment_id"] == "reply-1"
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    # A re-read too stale to show the reply ends on the operator's comment, so the
    # digest before it is not the conversation the reply followed: that reply is
    # tombstoned as one of an unread conversation (PRRT_kwDOSJAM6s6vc8NC).
    tombstoned = followed if viewer_bodies[-1].endswith("reply") else ""
    for kept_state in (state, _load_state(None, ws)):
        assert kept_state.threads_addressed_ids[tid] == "needs_human"
        kept = read_item_disposition(kept_state, tid)
        assert kept is not None and kept.reply_comment_id is None
        assert _disposition_retired_key(tid, tombstoned) in kept_state.threads_addressed_ids
    action = decide(_status(_PUSHED, (thread,)), state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)


@pytest.mark.parametrize(
    ("viewer_bodies", "superseded", "before_reply"),
    [
        (("earlier-reply", "reply"), 0, "d" * 64),
        (("earlier-reply", "reply"), 1, "d" * 64),
        ((_OPERATOR_ASK, "earlier-reply", "reply"), 0, "d" * 64),
        (("reply", _OPERATOR_ASK), 0, ""),
    ],
    ids=[
        "awf-reply-precedes",
        "superseded-awf-reply-precedes",
        "current-awf-reply-followed-the-operator",
        "reply-not-newest",
    ],
)
async def test_fix_cycle_resolves_when_no_unread_own_identity_feedback_precedes_the_reply(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    viewer_bodies: tuple[str, ...],
    superseded: int,
    before_reply: str,
) -> None:
    # Only unmarked feedback by AWF's identity since its previous reply that is
    # still current is unread: that earlier reply passed this check (or parked the
    # thread for a human) over the same conversation, for what precedes it, and a
    # comment after the reply is one the gate already refuses the reply as answering.
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_own_identity_answered"
    thread = _commented_thread(tid, _ASK)
    marker = _MARKER.format(cls="false_positive", tid=tid)
    bodies = tuple(marker if body.endswith("reply") else body for body in viewer_bodies)
    _serve_live_thread(
        gh,
        monkeypatch,
        tid,
        (_ASK,),
        before_reply=before_reply,
        viewer_bodies=bodies,
        viewer_ids=_own_comment_ids(viewer_bodies),
        superseded=superseded,
    )

    _, state, _ = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=lambda s, t: seed_resolvable_disposition(s, t.thread_id, "false_positive"),
        thread=thread,
        settle_threads=(thread,),
    )

    assert [event[0] for event in gh.events] == ["reply", "resolve"]
    assert state.threads_addressed_ids[tid] == "false_positive"


@pytest.mark.parametrize(
    ("viewer_bodies", "events", "verdict"),
    [
        ((_OPERATOR_ASK, "reply"), ["reply"], "needs_human"),
        (("reply",), ["reply", "resolve"], "false_positive"),
    ],
    ids=["unread-own-identity-feedback", "no-listed-copy-to-compare"],
)
async def test_fix_cycle_re_reads_a_reply_to_a_thread_the_settle_feed_does_not_list(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    viewer_bodies: tuple[str, ...],
    events: list[str],
    verdict: str,
) -> None:
    # PRRT_kwDOSJAM6s6vc8M9: with no listed copy of the thread the confirm was
    # skipped, so the reply was resolved, and later certified, over the operator's
    # unmarked feedback. That check needs only the live thread; the outside
    # comments, with no listed copy to compare them with, prove no difference.
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_unlisted_thread"
    marker = _MARKER.format(cls="false_positive", tid=tid)
    audited = _serve_live_thread(
        gh,
        monkeypatch,
        tid,
        (_ASK, "and cover empty payloads too"),
        before_reply="f" * 64,
        viewer_bodies=tuple(marker if body == "reply" else body for body in viewer_bodies),
        viewer_ids=_own_comment_ids(viewer_bodies),
    )

    _, state, _ = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=lambda s, t: seed_resolvable_disposition(s, t.thread_id, "false_positive"),
        thread=_commented_thread(tid, _ASK),
    )

    assert [event[0] for event in gh.events] == events
    assert audited == [42]
    assert state.threads_addressed_ids[tid] == verdict


@pytest.mark.parametrize(
    "newest", ["reply", "earlier-reply"], ids=["shows-the-reply", "stale-re-read"]
)
async def test_a_landed_reply_keeps_the_conversation_it_followed_for_its_tombstone(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    newest: str,
) -> None:
    # PRRT_kwDOSJAM6s6vaq4v: the re-read right after the reply records the
    # conversation it followed, saved at once. A reopen before any audit bound the
    # reply then still tombstones that conversation, which outlives the record.
    # A re-read too stale to show the reply may end on an earlier AWF reply: the
    # digest before that one is not the reply's conversation, so none is recorded
    # and the reply is tombstoned as one of an unread conversation
    # (PRRT_kwDOSJAM6s6vc8NC, PRRT_kwDOSJAM6s6veO2F).
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_landed_followed"
    thread = _commented_thread(tid, _ASK)
    before_reply = "e" * 64
    followed = before_reply if newest == "reply" else None
    _serve_live_thread(
        gh,
        monkeypatch,
        tid,
        (_ASK,),
        before_reply=before_reply,
        viewer_bodies=(_MARKER.format(cls="false_positive", tid=tid),),
        viewer_ids=_own_comment_ids((newest,)),
    )

    workspace_id, state, _ = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=lambda s, t: seed_resolvable_disposition(s, t.thread_id, "false_positive"),
        thread=thread,
        settle_threads=(thread,),
    )

    assert [event[0] for event in gh.events] == ["reply", "resolve"]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    saved = read_item_disposition(_load_state(None, ws), tid)
    assert saved is not None
    assert (saved.reply_comment_id, saved.resolved, saved.reply_conversation_digest) == (
        "reply-1",
        True,
        followed,
    )

    assert retire_reopened_dispositions(
        state, _status(_PUSHED, (thread,)), workspace_id=workspace_id
    ) == [tid]

    assert _disposition_retired_key(tid, followed or "") in state.threads_addressed_ids
    retired = read_item_disposition(state, tid)
    assert retired is not None and retired.reply_conversation_digest is None


async def test_fix_cycle_parks_when_the_thread_cannot_be_re_read_after_the_reply(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_feedback_unreadable"
    thread = _commented_thread(tid, _ASK)
    fault = GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502")
    _serve_live_thread(gh, monkeypatch, tid, (_ASK,), error=fault)

    workspace_id, state, result = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=lambda s, t: seed_resolvable_disposition(s, t.thread_id, "false_positive"),
        thread=thread,
        settle_threads=(thread,),
    )

    assert [event[0] for event in gh.events] == ["reply"]
    assert result.failed is False
    assert state.threads_addressed_ids[tid] == "needs_human"
    reason = state.threads_addressed_ids["__needs_human_reason__:" + tid]
    assert reason.startswith("REVIEW_THREAD_AUDIT_UNAVAILABLE:")
    parked = [
        e
        for e in await _resolution_events(factory, workspace_id)
        if e["action"] == "resolve_thread"
    ]
    assert [(e["outcome"], e["reason_code"]) for e in parked] == [
        ("needs_human", "REVIEW_THREAD_AUDIT_UNAVAILABLE")
    ]
    assert parked[0]["evidence"]["disposition"]["reply_comment_id"] == "reply-1"
    assert parked[0]["evidence"]["forge_reason_code"] == fault.reason_code


async def test_a_reply_no_re_read_confirmed_stays_saved_pending(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PRRT_kwDOSJAM6s6veO2C: a fresh reply is saved pending until its re-read
    # succeeds, so a re-read fault's park (or a restart before the re-read) leaves
    # nothing the gate would certify once the shared identity resolves the thread.
    gh = OrderedForge(FakeCommandRunner())
    tid = "PRRT_test_feedback_unread_park"
    thread = _commented_thread(tid, _ASK)
    fault = GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502")
    workspace_ids: list[str] = []
    saved_at_re_read: list[bool] = []

    async def _capture_workspace(_runner: object, workspace_id: str) -> None:
        workspace_ids.append(workspace_id)

    async def _audit(*, repo: RepoRef, pr_number: int, retry: bool = True) -> ReviewThreadAudit:
        del repo, pr_number, retry
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_ids[0])
        assert ws is not None
        saved = read_item_disposition(_load_state(None, ws), tid)
        assert saved is not None and saved.reply_comment_id == "reply-1"
        saved_at_re_read.append(saved.reread_pending)
        raise fault

    monkeypatch.setattr(gh, "fetch_review_thread_audit", _audit)

    workspace_id, state, _ = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=lambda s, t: seed_resolvable_disposition(s, t.thread_id, "false_positive"),
        thread=thread,
        before_cycle=_capture_workspace,
        settle_threads=(thread,),
    )

    assert saved_at_re_read == [True]
    assert state.threads_addressed_ids[tid] == "needs_human"
    parked = read_item_disposition(state, tid)
    assert parked is not None
    assert (parked.reply_comment_id, parked.reread_pending) == ("reply-1", True)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    saved = read_item_disposition(_load_state(None, ws), tid)
    assert saved is not None
    assert (saved.reply_comment_id, saved.reread_pending) == ("reply-1", True)
