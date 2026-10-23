"""READY_TO_MERGE / merge disposition gate (#1018).

"Inline comments resolved" used to mean only "zero unresolved threads", so a
thread AWF resolved silently looked identical to a handled one. Before the cert
(release monitors) or the merge (feature monitors) AWF now re-enumerates every
review thread and requires each thread resolved by its own identity to carry an
AWF disposition marker. A failure is ``NotifyHuman`` with a reason code listing
the thread ids — never a workspace failure.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import AuditedReviewThread, ReviewThreadAudit
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.feedback_policy import review_thread_body_hash, review_thread_body_state_key
from awf.runtime.monitor_state_keys import _disposition_retired_key, _disposition_unverified_key
from awf.runtime.pr_monitor import (
    AddressComments,
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewThread,
    ReviewThreadComment,
    decide,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import VerdictResult
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    read_item_disposition,
    record_item_disposition,
    retire_reopened_dispositions,
    sync_item_disposition,
)
from awf.runtime.pr_monitor_runner.thread_disposition_gate import (
    review_thread_disposition_gate,
)
from awf.service.pr_monitor_adoption_seed import seedable_monitor_state
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._merge_methods_fixtures import (
    _TEST_DEFAULT_BASE_BRANCH,
    _TEST_PR_NUMBER,
    _TEST_REPO,
    _execute_merge,
    _mergeable_status,
    _MergeMethodClient,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._thread_disposition_fixtures import settled_baseline_state

pytestmark = pytest.mark.unit

_REPO = RepoRef(owner="acme", name="widgets")
_REPO_URL = "git@github.com:acme/widgets.git"
_VIEWER = "awf-operator"
_SILENT = "PRRT_test_silent_p1"
_FRESH = "PRRT_test_fresh_p1"
_AUDIT = "workspace.audit.comment_resolution"


def _marker(cls: str, tid: str) -> str:
    return (
        f"<!-- AWF:thread-disposition:v1 class={cls} thread={tid} -->\n**AWF disposition: {cls}.**"
    )


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _green_status() -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


class _AuditForge(DefaultMergeMethodGitHubClient):
    """Serves a canned audit (or raises) and records posted PR comments."""

    def __init__(
        self,
        runner: FakeCommandRunner,
        *,
        audit: ReviewThreadAudit | None = None,
        error: ForgeClientError | None = None,
    ) -> None:
        super().__init__(runner)
        self.audit = audit or ReviewThreadAudit(supported=True, viewer_login=_VIEWER)
        self.error = error
        self.comments: list[str] = []
        self.merges: list[str] = []

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int, retry: bool = True
    ) -> ReviewThreadAudit:
        assert repo == _REPO and pr_number == 42
        if self.error is not None:
            raise self.error
        return self.audit

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        del repo, pr_number
        self.comments.append(body)

    async def merge_pr(
        self, *, repo: RepoRef, pr_number: int, method: str = "squash", delete_branch: bool = True
    ) -> str:
        del repo, pr_number, delete_branch
        self.merges.append(method)
        return "MERGESHA"


class _RealAudit(DefaultMergeMethodGitHubClient):
    """The real GraphQL audit over ``FakeCommandRunner`` responders."""

    fetch_review_thread_audit = GitHubClient.fetch_review_thread_audit


def _thread(
    tid: str,
    *,
    resolved_by: str | None = _VIEWER,
    bodies: tuple[str, ...] = (),
    resolved: bool = True,
    superseded: int = 0,
    external: tuple[str, ...] = (),
) -> AuditedReviewThread:
    return AuditedReviewThread(
        thread_id=tid,
        is_resolved=resolved,
        is_outdated=False,
        resolved_by=resolved_by,
        viewer_comment_bodies=bodies,
        superseded_viewer_comments=superseded,
        external_comment_bodies=external,
    )


async def _release_runner(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, gh: Any, *, auto_merge: bool = False
) -> tuple[str, Any]:
    workspace_id = await seed_monitoring_workspace(factory, auto_merge=auto_merge)
    runner = make_runner(
        factory=factory,
        cmd=gh._runner,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        auto_merge=auto_merge,
        gh=gh,
    )
    return workspace_id, runner


async def _execute(runner: Any, workspace_id: str, action: Any, state: MonitorState) -> Any:
    return await runner._execute(
        action=action,
        workspace_id=workspace_id,
        repo_url=_REPO_URL,
        repo=_REPO,
        pr_number=42,
        status=_green_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=_REPO_URL,
        compose_project="proj",
        compose_file=Path("/tmp/compose.yml"),
        monitor_log=None,
    )


async def _gate(
    runner: Any, workspace_id: str, state: MonitorState, status: PRStatus | None = None
) -> Any:
    return await review_thread_disposition_gate(
        runner,
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        status=status or _green_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        monitor_log=None,
    )


async def _audit_events(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> list[Any]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == workspace_id, WorkspaceEvent.event_type == _AUDIT)
            .order_by(WorkspaceEvent.event_order)
        )
        return [
            row.payload
            for row in rows.scalars().all()
            if row.payload.get("action") == "review_thread_disposition_audit"
        ]


async def _attention(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> tuple[Any, ...]:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    return ws.awaiting_human_since, ws.awaiting_human_reason, ws.status


def _graphql_page(threads: list[dict[str, Any]], *, cursor: str | None) -> str:
    return json.dumps(
        {
            "data": {
                "viewer": {"login": _VIEWER},
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": threads,
                            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                        }
                    }
                },
            }
        }
    )


def _graphql_thread(tid: str, comments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": tid,
        "isResolved": True,
        "isOutdated": False,
        "resolvedBy": {"login": _VIEWER},
        "comments": {"nodes": comments, "pageInfo": {"hasNextPage": False, "endCursor": None}},
    }


async def test_cert_refused_when_awf_resolved_thread_lacks_marker_across_pagination(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    page1 = [
        _graphql_thread(
            f"PRRT_test_{i:03d}",
            [
                {"body": "please fix", "viewerDidAuthor": False},
                {"body": _marker("fixed", f"PRRT_test_{i:03d}"), "viewerDidAuthor": True},
            ],
        )
        for i in range(100)
    ]
    page2 = [
        _graphql_thread(
            _SILENT, [{"body": "**P1** worker crashes on None", "viewerDidAuthor": False}]
        )
    ]

    def _is_audit(argv: list[str]) -> bool:
        return any("ReviewThreadDispositionAudit" in arg for arg in argv)

    cmd.respond_when(
        lambda a: _is_audit(a) and "cursor=c1" in a, stdout=_graphql_page(page2, cursor=None)
    )
    cmd.respond_when(_is_audit, stdout=_graphql_page(page1, cursor="c1"))
    cmd.respond_when(lambda a: a[:3] == ["gh", "pr", "comment"])
    gh = _RealAudit(cmd)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    state = settled_baseline_state()

    terminal = await _execute(runner, workspace_id, NotifyHuman(), state)

    assert terminal is False
    comments = [c.args for c in cmd.calls if c.args[:3] == ["gh", "pr", "comment"]]
    assert len(comments) == 1
    body = comments[0][comments[0].index("--body") + 1]
    assert "is ready to merge" not in body
    assert "REVIEW_THREAD_DISPOSITION_UNVERIFIED" in body
    assert _SILENT in body
    assert not any(c.args[:3] == ["gh", "pr", "merge"] for c in cmd.calls)
    since, reason, status = await _attention(factory, workspace_id)
    assert status == WorkspaceStatus.monitoring_pr.value
    assert since is not None and reason is not None and _SILENT in reason
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]
    events = await _audit_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0]["outcome"] == "blocked"
    assert events[0]["reason_code"] == "REVIEW_THREAD_DISPOSITION_UNVERIFIED"
    assert events[0]["evidence"]["thread_ids"] == [_SILENT]
    assert events[0]["evidence"]["disposition_counts"] == {"fixed": 100}


async def test_cert_posts_per_class_counts_when_all_markers_present(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audit = ReviewThreadAudit(
        supported=True,
        viewer_login=_VIEWER,
        threads=(
            _thread("PRRT_test_f1", bodies=(_marker("fixed", "PRRT_test_f1"),)),
            _thread("PRRT_test_f2", bodies=("unrelated", _marker("fixed", "PRRT_test_f2"))),
            _thread("PRRT_test_fp", bodies=(_marker("false_positive", "PRRT_test_fp"),)),
            _thread("PRRT_test_d", bodies=(_marker("deferred", "PRRT_test_d"),)),
            _thread("PRRT_test_b", bodies=(_marker("fixed_branch_evidence", "PRRT_test_b"),)),
            _thread("PRRT_test_o1", resolved_by="review-bot"),
            _thread("PRRT_test_o2", resolved_by="review-bot"),
            _thread("PRRT_test_o3", resolved_by=None),
            _thread("PRRT_test_open", resolved=False),
        ),
    )
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    terminal = await _execute(runner, workspace_id, NotifyHuman(), settled_baseline_state())

    assert terminal is False
    assert len(gh.comments) == 1
    assert "is ready to merge" in gh.comments[0]
    assert (
        "AWF dispositions: fixed 2, fixed (branch evidence) 1, false positive 1, deferred 1"
        "; resolved by others 3."
    ) in gh.comments[0]
    assert await _audit_events(factory, workspace_id) == []


async def test_cert_reposts_at_same_head_when_audited_counts_change(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A no-commit FALSE POSITIVE / DEFER changes the counts but not the head.

    The cert renders the audited counts, so they are part of its dedupe identity:
    an unchanged audit stays deduped, a changed one re-posts the certificate.
    """
    audit = ReviewThreadAudit(
        supported=True,
        viewer_login=_VIEWER,
        threads=(_thread("PRRT_test_f1", bodies=(_marker("fixed", "PRRT_test_f1"),)),),
    )
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    state = settled_baseline_state()

    await _execute(runner, workspace_id, NotifyHuman(), state)
    await _execute(runner, workspace_id, NotifyHuman(), state)
    gh.audit = ReviewThreadAudit(
        supported=True,
        viewer_login=_VIEWER,
        threads=(
            *audit.threads,
            _thread("PRRT_test_fp", bodies=(_marker("false_positive", "PRRT_test_fp"),)),
        ),
    )
    await _execute(runner, workspace_id, NotifyHuman(), state)
    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert len(gh.comments) == 2
    assert all("is ready to merge at commit `abc1234567`" in c for c in gh.comments)
    assert "fixed 1, fixed (branch evidence) 0, false positive 0," in gh.comments[0]
    assert "fixed 1, fixed (branch evidence) 0, false positive 1," in gh.comments[1]


@pytest.mark.parametrize(
    ("viewer", "resolved_by", "blocked"),
    [
        ("awf-operator", "AWF-Operator", True),  # case-insensitive identity match
        ("awf-operator", "human-reviewer", False),  # someone else's disposition
        ("dmitri", "dmitri", True),  # AWF runs as the operator: marker, not login
        ("awf-operator", None, False),  # deleted / app account
    ],
)
async def test_gate_identity_edges(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    viewer: str,
    resolved_by: str | None,
    blocked: bool,
) -> None:
    audit = ReviewThreadAudit(
        supported=True,
        viewer_login=viewer,
        threads=(_thread(_SILENT, resolved_by=resolved_by),),
    )
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    state = settled_baseline_state()

    result = await _gate(runner, workspace_id, state)

    assert result.retry is False
    if blocked:
        assert result.blocker is not None
        assert result.blocker.startswith("REVIEW_THREAD_DISPOSITION_UNVERIFIED:")
        assert _SILENT in result.blocker
        assert result.count_rows is None
    else:
        assert result.blocker is None
        assert result.count_rows is not None
        assert dict(result.count_rows)["resolved by others"] == 1


async def test_gate_rejects_a_marker_older_than_the_threads_newest_feedback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A dispositioned thread is reopened, gets new reviewer feedback and is then
    # resolved silently by AWF's (shared) identity: its marker answered the earlier
    # feedback, not this resolution. A re-triage reply after the feedback counts.
    retriaged = "PRRT_test_retriaged"
    audit = ReviewThreadAudit(
        supported=True,
        viewer_login=_VIEWER,
        threads=(
            _thread(_SILENT, bodies=(_marker("fixed", _SILENT),), superseded=1),
            _thread(
                retriaged,
                bodies=(_marker("false_positive", retriaged), _marker("fixed", retriaged)),
                superseded=1,
            ),
        ),
    )
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    result = await _gate(runner, workspace_id, settled_baseline_state())

    assert result.blocker is not None
    assert result.blocker.startswith("REVIEW_THREAD_DISPOSITION_UNVERIFIED:")
    assert _SILENT in result.blocker and retriaged not in result.blocker
    events = await _audit_events(factory, workspace_id)
    assert [e["evidence"]["thread_ids"] for e in events] == [[_SILENT]]
    assert events[0]["evidence"]["disposition_counts"] == {"fixed": 1}


async def test_reopen_retires_a_certified_disposition_so_a_silent_re_resolve_blocks(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # AWF replied FIXED and resolved; a gate audit certified the reply. The
    # reviewer reopens the thread without commenting and the shared identity
    # resolves it again: the reply answered the resolution the reopen ended.
    state = settled_baseline_state()
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=_SILENT,
            disposition_class="fixed",
            verdict="fix_committed",
            commits=("c" * 40,),
            reply_comment_id="reply-1",
        ),
    )
    resolved = replace(
        _thread(_SILENT, bodies=(_marker("fixed", _SILENT),)),
        conversation_digest="feedback|reply-1",
        conversation_digest_before_reply="feedback",
    )
    audit = ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(resolved,))
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    certified = await _gate(runner, workspace_id, state)
    assert certified.blocker is None and certified.count_rows is not None
    assert dict(certified.count_rows)["fixed"] == 1

    gh.audit = replace(audit, threads=(replace(resolved, is_resolved=False, resolved_by=None),))
    assert (await _gate(runner, workspace_id, state)).blocker is None
    retired = read_item_disposition(state, _SILENT)
    # Retired, not forgotten: the thread's next AWF resolve owes a fresh reply.
    assert retired is not None and retired.reply_comment_id is None

    gh.audit = audit
    blocked = await _gate(runner, workspace_id, state)

    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]


def _certified_fixed_thread(state: MonitorState) -> tuple[AuditedReviewThread, ReviewThreadAudit]:
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=_SILENT,
            disposition_class="fixed",
            verdict="fix_committed",
            commits=("c" * 40,),
            reply_comment_id="reply-1",
        ),
    )
    resolved = replace(
        _thread(_SILENT, bodies=(_marker("fixed", _SILENT),)),
        conversation_digest="feedback|reply-1",
        conversation_digest_before_reply="feedback",
    )
    return resolved, ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(resolved,))


async def test_a_retired_reply_never_certifies_once_its_thread_has_no_record(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = settled_baseline_state()
    resolved, audit = _certified_fixed_thread(state)
    reopened = replace(audit, threads=(replace(resolved, is_resolved=False, resolved_by=None),))
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    assert (await _gate(runner, workspace_id, state)).blocker is None
    gh.audit = reopened
    await _gate(runner, workspace_id, state)  # the reopen retires the FIXED reply
    # Its re-triage leaves the thread open, which drops the record altogether.
    sync_item_disposition(state, _SILENT, VerdictResult(verdict="needs_human", reason="ask"))
    assert read_item_disposition(state, _SILENT) is None

    gh.audit = audit
    blocked = await _gate(runner, workspace_id, state)

    # The retired FIXED reply is still the newest marker; it certifies nothing.
    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert gh.thread_replies == []


async def test_a_reopen_only_a_poll_lists_retires_the_bound_reply(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = settled_baseline_state()
    _, audit = _certified_fixed_thread(state)
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    assert (await _gate(runner, workspace_id, state)).blocker is None
    # A poll lists the thread open again while no gate runs (a NotifyHuman or
    # WaitForCI window); the shared identity then resolves it silently.
    listed = ReviewThread(
        thread_id=_SILENT, path="widgets/core.py", line=1, body_excerpt="x", author="bot"
    )

    assert retire_reopened_dispositions(
        state,
        replace(_green_status(), unresolved_inline_threads=(listed,)),
        workspace_id=workspace_id,
    ) == [_SILENT]
    blocked = await _gate(runner, workspace_id, state)

    assert blocked.blocker is not None and _SILENT in blocked.blocker
    # A disposition no audit has bound yet may be a failed resolve's: it stays.
    unbound = ThreadDisposition(
        item_id=_FRESH, disposition_class="fixed", verdict="fix_committed", reply_comment_id="r"
    )
    record_item_disposition(state, unbound)
    outdated = replace(listed, thread_id=_FRESH)
    assert (
        retire_reopened_dispositions(
            state,
            replace(_green_status(), outdated_unresolved_inline_threads=(outdated,)),
            workspace_id=workspace_id,
        )
        == []
    )
    assert read_item_disposition(state, _FRESH) == unbound


@pytest.mark.parametrize("reply_comment_id", [None, "reply-1"])
@pytest.mark.parametrize("audit_sees_reopen", [False, True])
async def test_reopening_a_flagged_thread_re_triages_it(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    reply_comment_id: str | None,
    audit_sees_reopen: bool,
) -> None:
    # Reopening is how an operator clears REVIEW_THREAD_DISPOSITION_UNVERIFIED: AWF
    # re-triages the thread through the normal fix cycle, which posts a fresh
    # disposition before resolving it. A FIXED thread whose comments did not change
    # would not re-enter triage by itself, so the poll that lists the reopen clears
    # its verdict, even when a gate audit saw the reopen first. Here the
    # operator resolved it by hand under AWF's account, before AWF's resolve or
    # after its reply (to a resolve that failed) was superseded by newer feedback.
    comments = (ReviewThreadComment(comment_id="1", body="P1: crash on None"),)
    listed = ReviewThread(
        thread_id=_SILENT, path="a.py", line=1, body_excerpt="P1", comments=comments
    )
    untouched = replace(listed, thread_id=_FRESH)
    state = settled_baseline_state()
    for thread in (listed, untouched):
        state.mark_addressed(thread.thread_id, "fix_committed")
        state.mark_addressed(
            review_thread_body_state_key(thread.thread_id), review_thread_body_hash(thread)
        )
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=_SILENT,
            disposition_class="fixed",
            verdict="fix_committed",
            commits=("c" * 40,),
            reply_comment_id=reply_comment_id,
            reply_conversation_digest="b" * 64 if reply_comment_id else None,
        ),
    )
    marker = (_marker("fixed", _SILENT),) if reply_comment_id else ()
    flagged = _thread(_SILENT, bodies=marker, superseded=len(marker), external=(comments[0].body,))
    gh = _AuditForge(
        FakeCommandRunner(),
        audit=ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(flagged,)),
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    blocked = await _gate(runner, workspace_id, state)
    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert "reopen it: AWF re-triages it through the normal fix cycle" in blocked.blocker
    reopened = replace(_green_status(), unresolved_inline_threads=(listed, untouched))
    config = MonitorConfig(auto_merge=False)
    assert not isinstance(decide(reopened, state, config), AddressComments)
    if audit_sees_reopen:
        gh.audit = ReviewThreadAudit(
            supported=True,
            viewer_login=_VIEWER,
            threads=(replace(flagged, is_resolved=False, resolved_by=None),),
        )
        assert (await _gate(runner, workspace_id, state, reopened)).blocker is None
        assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]

    retire_reopened_dispositions(state, reopened, workspace_id=workspace_id)
    action = decide(reopened, state, config)

    assert isinstance(action, AddressComments)
    assert [t.thread_id for t in action.threads] == [_SILENT]
    # Awaiting that re-triage, which this poll step never clears again.
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)] == "reopened"
    # No record left to reuse: the re-triage records, and replies with, a fresh one.
    assert read_item_disposition(state, _SILENT) is None
    retired = _disposition_retired_key(_SILENT, "b" * 64) in state.threads_addressed_ids
    assert retired is (reply_comment_id is not None)
    assert state.threads_addressed_ids[_FRESH] == "fix_committed"  # never flagged


@pytest.mark.parametrize("seen_by", ["poll", "audit"])
async def test_a_reopen_retires_a_reply_whose_resolve_landed_before_any_audit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    seen_by: str,
) -> None:
    # PRRT_kwDOSJAM6s6vZ4nJ: AWF replied and its resolve landed, but no gate audit
    # bound the reply yet (an outdated thread resolved while CI is pending). The
    # reviewer reopens the unchanged thread and the shared identity resolves it
    # again: the landed resolve is what the reopen ended, so its reply is retired.
    state = settled_baseline_state()
    landed = ThreadDisposition(
        item_id=_SILENT,
        disposition_class="fixed",
        verdict="fix_committed",
        commits=("c" * 40,),
        reply_comment_id="reply-1",
        resolved=True,
    )
    record_item_disposition(state, landed)
    resolved = replace(
        _thread(_SILENT, bodies=(_marker("fixed", _SILENT),)),
        conversation_digest="feedback|reply-1",
        conversation_digest_before_reply="feedback",
    )
    audit = ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(resolved,))
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    if seen_by == "poll":
        listed = ReviewThread(
            thread_id=_SILENT, path="widgets/core.py", line=1, body_excerpt="x", author="bot"
        )
        assert retire_reopened_dispositions(
            state,
            replace(_green_status(), outdated_unresolved_inline_threads=(listed,)),
            workspace_id=workspace_id,
        ) == [_SILENT]
    else:
        gh.audit = replace(audit, threads=(replace(resolved, is_resolved=False, resolved_by=None),))
        assert (await _gate(runner, workspace_id, state)).blocker is None
    retired = read_item_disposition(state, _SILENT)
    assert retired is not None
    assert (retired.reply_comment_id, retired.resolved) == (None, False)

    gh.audit = audit
    blocked = await _gate(runner, workspace_id, state)

    assert blocked.blocker is not None and _SILENT in blocked.blocker


@pytest.mark.parametrize("seen_by", ["poll", "audit"])
async def test_a_reopen_before_any_audit_leaves_a_tombstone_that_crosses_adoption(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    seen_by: str,
) -> None:
    # PRRT_kwDOSJAM6s6vaq4v: the reply's resolve landed and the thread reopened
    # before any audit bound it. An adoption onto a moved head then drops the
    # code-dependent record, so only a tombstone of the conversation the reply
    # followed (read right after it was posted) keeps the shared identity's silent
    # re-resolve from certifying the historical marker.
    followed = "b" * 64
    state = settled_baseline_state()
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=_SILENT,
            disposition_class="fixed",
            verdict="fix_committed",
            commits=("c" * 40,),
            reply_comment_id="reply-1",
            resolved=True,
            reply_conversation_digest=followed,
        ),
    )
    resolved = replace(
        _thread(_SILENT, bodies=(_marker("fixed", _SILENT),)),
        conversation_digest="a" * 64,
        conversation_digest_before_reply=followed,
    )
    audit = ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(resolved,))
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    if seen_by == "poll":
        listed = ReviewThread(
            thread_id=_SILENT, path="widgets/core.py", line=1, body_excerpt="x", author="bot"
        )
        retire_reopened_dispositions(
            state,
            replace(_green_status(), unresolved_inline_threads=(listed,)),
            workspace_id=workspace_id,
        )
    else:
        gh.audit = replace(audit, threads=(replace(resolved, is_resolved=False, resolved_by=None),))
        await _gate(runner, workspace_id, state)
    retired = read_item_disposition(state, _SILENT)
    assert retired is not None and retired.reply_conversation_digest is None
    adopted = MonitorState()
    for key, value in seedable_monitor_state(
        state.threads_addressed_ids, head_continuity=False
    ).items():
        adopted.mark_addressed(key, value)
    assert read_item_disposition(adopted, _SILENT) is None

    gh.audit = audit
    blocked = await _gate(runner, workspace_id, adopted)

    assert blocked.blocker is not None and _SILENT in blocked.blocker


async def test_an_audit_binds_a_landed_resolve_it_sees_standing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    state = settled_baseline_state()
    resolved, audit = _certified_fixed_thread(state)
    landed = read_item_disposition(state, _SILENT)
    assert landed is not None
    record_item_disposition(state, replace(landed, resolved=True))
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    certified = await _gate(runner, workspace_id, state)

    assert certified.blocker is None and certified.count_rows is not None
    assert dict(certified.count_rows)["fixed"] == 1
    bound = read_item_disposition(state, _SILENT)
    assert bound is not None
    assert (bound.reply_comment_id, bound.resolved, bound.conversation_digest) == (
        "reply-1",
        True,
        resolved.conversation_digest_before_reply,
    )


async def test_an_unconfirmed_reply_never_certifies(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A reply whose fail-fast re-read under the merge lock faulted was never checked
    # against the conversation it follows (PRRT_kwDOSJAM6s6vbU2K), so a silent
    # resolve by AWF's identity on it blocks until a re-read confirms it.
    state = settled_baseline_state()
    _, audit = _certified_fixed_thread(state)
    posted = read_item_disposition(state, _SILENT)
    assert posted is not None
    record_item_disposition(state, replace(posted, reread_pending=True))
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    blocked = await _gate(runner, workspace_id, state)

    assert blocked.blocker is not None and _SILENT in blocked.blocker


async def test_an_unconfirmed_reply_never_certifies_after_adoption_onto_a_moved_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # PRRT_kwDOSJAM6s6vculv: a moved head drops a code-dependent record, and with no
    # record the gate takes the marker as the evidence. The shared identity's
    # silent resolve of a reply no re-read ever checked must still block there.
    state = settled_baseline_state()
    _, audit = _certified_fixed_thread(state)
    posted = read_item_disposition(state, _SILENT)
    assert posted is not None
    record_item_disposition(state, replace(posted, reread_pending=True))
    adopted = MonitorState()
    for key, value in seedable_monitor_state(
        state.threads_addressed_ids, head_continuity=False
    ).items():
        adopted.mark_addressed(key, value)
    gh = _AuditForge(FakeCommandRunner(), audit=audit)
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    blocked = await _gate(runner, workspace_id, adopted)

    assert blocked.blocker is not None and _SILENT in blocked.blocker


async def test_gate_rejects_a_marker_older_than_the_shared_identitys_own_feedback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # AWF can run as the operator's own account, so the operator's follow-up in a
    # dispositioned thread is viewer-authored too. AWF only posts marker replies,
    # so that unmarked comment is newer feedback: the earlier marker no longer
    # covers a silent resolution, while a re-triage marker after it does.
    retriaged = "PRRT_test_retriaged"
    feedback = {"body": "P1: crash on None", "viewerDidAuthor": False}
    follow_up = {"body": "also handle the empty list", "viewerDidAuthor": True}
    threads = [
        _graphql_thread(
            _SILENT,
            [feedback, {"body": _marker("fixed", _SILENT), "viewerDidAuthor": True}, follow_up],
        ),
        _graphql_thread(
            retriaged,
            [
                feedback,
                {"body": _marker("fixed", retriaged), "viewerDidAuthor": True},
                follow_up,
                {"body": _marker("false_positive", retriaged), "viewerDidAuthor": True},
            ],
        ),
    ]
    cmd = FakeCommandRunner()
    cmd.respond_when(
        lambda a: any("ReviewThreadDispositionAudit" in arg for arg in a),
        stdout=_graphql_page(threads, cursor=None),
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, _RealAudit(cmd))

    result = await _gate(runner, workspace_id, settled_baseline_state())

    assert result.blocker is not None
    assert result.blocker.startswith("REVIEW_THREAD_DISPOSITION_UNVERIFIED:")
    assert _SILENT in result.blocker and retriaged not in result.blocker
    events = await _audit_events(factory, workspace_id)
    assert [e["evidence"]["thread_ids"] for e in events] == [[_SILENT]]
    assert events[0]["evidence"]["disposition_counts"] == {"false_positive": 1}


async def test_gate_missing_viewer_login_fails_closed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditForge(
        FakeCommandRunner(),
        audit=ReviewThreadAudit(supported=True, viewer_login=None, threads=(_thread(_SILENT),)),
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    result = await _gate(runner, workspace_id, settled_baseline_state())

    assert result.blocker is not None
    assert result.blocker.startswith("REVIEW_THREAD_AUDIT_UNAVAILABLE:")
    events = await _audit_events(factory, workspace_id)
    assert [(e["outcome"], e["reason_code"]) for e in events] == [
        ("failed", "REVIEW_THREAD_AUDIT_UNAVAILABLE")
    ]


async def test_gate_records_one_audit_event_per_newly_flagged_set(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditForge(
        FakeCommandRunner(),
        audit=ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(_thread(_SILENT),)),
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    state = settled_baseline_state()

    first = await _gate(runner, workspace_id, state)
    second = await _gate(runner, workspace_id, state)

    assert first.blocker == second.blocker
    assert len(await _audit_events(factory, workspace_id)) == 1


@pytest.mark.parametrize(
    ("external", "listed_in", "listed_comments", "retry"),
    [
        (("P1",), None, (), True),
        (("P1",), "unresolved_inline_threads", ("P1",), False),
        (("P1",), "outdated_unresolved_inline_threads", ("P1",), False),
        (("P1", "P2"), "unresolved_inline_threads", ("P1",), True),  # replied to since: a
        (("P1", "P2"), "outdated_unresolved_inline_threads", ("P1",), True),  # fix_committed
        (("P1 v2",), "unresolved_inline_threads", ("P1",), True),  # thread stays listed;
        (("P1 v2",), "outdated_unresolved_inline_threads", ("P1",), True),  # edited since
        ((), None, (), False),  # only AWF's identity commented: no status lists it
    ],
)
async def test_gate_retries_on_unresolved_feedback_the_decided_status_never_listed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    external: tuple[str, ...],
    listed_in: str | None,
    listed_comments: tuple[str, ...],
    retry: bool,
) -> None:
    """PRRT_kwDOSJAM6s6vVxR0 / PRRT_kwDOSJAM6s6vWLkv: a thread opened, reopened, replied
    to, or edited after the status ``decide`` acted on is unresolved in the audit with
    other feedback than it listed; passing would certify or merge over it."""
    gh = _AuditForge(
        FakeCommandRunner(),
        audit=ReviewThreadAudit(
            supported=True,
            viewer_login=_VIEWER,
            threads=(
                _thread("PRRT_test_ok", bodies=(_marker("fixed", "PRRT_test_ok"),)),
                _thread(_FRESH, resolved=False, external=external),
            ),
        ),
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    status = _green_status()
    if listed_in is not None:
        comments = tuple(
            ReviewThreadComment(comment_id=str(n), body=body)
            for n, body in enumerate(listed_comments)
        )
        listed = (
            ReviewThread(
                thread_id=_FRESH, path="a.py", line=1, body_excerpt="P1", comments=comments
            ),
        )
        status = replace(status, **{listed_in: listed})

    result = await _gate(runner, workspace_id, settled_baseline_state(), status)

    assert result.retry is retry
    assert result.blocker is None
    assert (result.count_rows is None) is retry
    assert await _audit_events(factory, workspace_id) == []


async def test_cert_waits_for_a_poll_that_lists_feedback_opened_after_its_snapshot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditForge(
        FakeCommandRunner(),
        audit=ReviewThreadAudit(
            supported=True,
            viewer_login=_VIEWER,
            threads=(_thread(_FRESH, resolved=False, external=("P1",)),),
        ),
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    terminal = await _execute(runner, workspace_id, NotifyHuman(), settled_baseline_state())

    assert terminal is False
    assert gh.comments == []  # no READY_TO_MERGE cert over the unseen feedback


class _AuditMergeClient(_MergeMethodClient):
    """The merge-method double, serving a canned review-thread audit."""

    def __init__(self, audit: ReviewThreadAudit) -> None:
        super().__init__(merge_results=["MERGESHA123"])
        self.audit = audit

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int, retry: bool = True
    ) -> ReviewThreadAudit:
        del repo, pr_number, retry
        return self.audit


async def test_feature_monitor_merge_refused_by_gate(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditMergeClient(
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(_thread(_SILENT),))
    )

    terminal, state, _, workspace_id = await _execute_merge(
        factory=factory, tmp_path=tmp_path, gh=gh, state=settled_baseline_state()
    )

    assert terminal is False
    assert gh.merge_calls == []
    assert len(gh.comments) == 1
    assert "REVIEW_THREAD_DISPOSITION_UNVERIFIED" in gh.comments[0]
    assert _SILENT in gh.comments[0]
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]
    _, _, status = await _attention(factory, workspace_id)
    assert status == WorkspaceStatus.monitoring_pr.value


async def test_feature_monitor_merge_proceeds_with_verified_audit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditMergeClient(
        ReviewThreadAudit(
            supported=True,
            viewer_login=_VIEWER,
            threads=(_thread("PRRT_test_ok", bodies=(_marker("fixed", "PRRT_test_ok"),)),),
        )
    )

    terminal, _, _, _ = await _execute_merge(factory=factory, tmp_path=tmp_path, gh=gh)

    assert terminal is True
    assert gh.merge_calls == ["squash"]


async def test_gate_transient_audit_fault_waits_and_does_not_notify(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditForge(
        FakeCommandRunner(),
        error=GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502"),
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    state = settled_baseline_state()

    terminal = await _execute(runner, workspace_id, NotifyHuman(), state)

    assert terminal is False
    assert gh.comments == []
    assert (
        state.threads_addressed_ids["__awf_forge_transient_retry_count:review_thread_audit"] == "1"
    )
    assert await _audit_events(factory, workspace_id) == []


async def test_gate_permanent_audit_fault_notifies_unavailable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditForge(
        FakeCommandRunner(),
        error=GitHubClientError(
            operation="gh api graphql", returncode=1, stderr="GraphQL: Could not resolve to a node"
        ),
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    terminal = await _execute(runner, workspace_id, NotifyHuman(), settled_baseline_state())

    assert terminal is False
    assert len(gh.comments) == 1
    assert "REVIEW_THREAD_AUDIT_UNAVAILABLE" in gh.comments[0]
    assert "is ready to merge" not in gh.comments[0]
    events = await _audit_events(factory, workspace_id)
    assert events[0]["evidence"]["forge_reason_code"] == "GITHUB_API_ERROR"


async def test_gate_bitbucket_unsupported_keeps_cert_text(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditForge(FakeCommandRunner(), audit=ReviewThreadAudit(supported=False))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    terminal = await _execute(runner, workspace_id, NotifyHuman(), settled_baseline_state())

    assert terminal is False
    assert len(gh.comments) == 1
    assert "is ready to merge" in gh.comments[0]
    assert "1. Inline comments resolved.\n" in gh.comments[0]
    assert "AWF dispositions" not in gh.comments[0]


async def test_audit_outage_after_ready_cert_at_head_is_log_only(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Once READY_TO_MERGE is posted for a head, an audit outage there only logs.

    The cert was earned on a complete audit at this head. A later poll that cannot
    re-enumerate the threads must not contradict it with a blocker comment or move
    the workspace's attention; it waits and re-polls.
    """
    gh = _AuditForge(FakeCommandRunner())
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    state = settled_baseline_state()
    await _execute(runner, workspace_id, NotifyHuman(), state)
    assert len(gh.comments) == 1 and "is ready to merge" in gh.comments[0]
    attention = await _attention(factory, workspace_id)
    slept = len(runner._deps.sleep.calls)

    gh.error = GitHubClientError(
        operation="gh api graphql", returncode=1, stderr="GraphQL: Could not resolve to a node"
    )
    terminal = await _execute(runner, workspace_id, NotifyHuman(), state)

    assert terminal is False
    assert len(gh.comments) == 1  # no REVIEW_THREAD_AUDIT_UNAVAILABLE comment
    assert await _attention(factory, workspace_id) == attention
    assert len(runner._deps.sleep.calls) == slept + 1  # one poll interval, no hot loop


async def test_audit_unavailable_notifies_once_across_polls_with_varying_errors(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A persistent audit outage must not post a new public PR comment every poll.

    Forge error text varies per request (request ids, addresses), so it stays out
    of the NotifyHuman message that keys the notification dedupe; it lands in the
    audit evidence, which is recorded once per outage.
    """
    gh = _AuditForge(
        FakeCommandRunner(),
        error=GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="GraphQL: Could not resolve to a node (request req-AAA111)",
        ),
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    state = settled_baseline_state()

    await _execute(runner, workspace_id, NotifyHuman(), state)
    gh.error = GitHubClientError(
        operation="gh api graphql",
        returncode=1,
        stderr="GraphQL: Could not resolve to a node (request req-BBB222)",
    )
    await _execute(runner, workspace_id, NotifyHuman(), state)

    assert len(gh.comments) == 1
    assert "REVIEW_THREAD_AUDIT_UNAVAILABLE" in gh.comments[0]
    assert "req-AAA111" not in gh.comments[0]
    events = await _audit_events(factory, workspace_id)
    assert len(events) == 1
    assert "req-AAA111" in events[0]["evidence"]["error_message"]

    # A successful audit ends the outage; a later one is recorded again.
    gh.error = None
    assert (await _gate(runner, workspace_id, state)).blocker is None
    gh.error = GitHubClientError(
        operation="gh api graphql", returncode=1, stderr="GraphQL: Could not resolve (req-CCC)"
    )
    assert (await _gate(runner, workspace_id, state)).blocker is not None
    assert len(await _audit_events(factory, workspace_id)) == 2


async def test_feature_merge_waits_on_transient_audit_fault(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    class _TransientAuditMergeClient(_AuditMergeClient):
        async def fetch_review_thread_audit(
            self, *, repo: RepoRef, pr_number: int, retry: bool = True
        ) -> ReviewThreadAudit:
            del repo, pr_number, retry
            raise GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502")

    gh = _TransientAuditMergeClient(ReviewThreadAudit(supported=True))

    terminal, state, sleep_fn, _ = await _execute_merge(factory=factory, tmp_path=tmp_path, gh=gh)

    assert terminal is False
    assert gh.merge_calls == []
    assert gh.comments == []
    assert sleep_fn.calls  # backed off before the next poll
    assert (
        state.threads_addressed_ids["__awf_forge_transient_retry_count:review_thread_audit"] == "1"
    )


class _SequencedAuditMergeClient(_AuditMergeClient):
    """Serves one audit (or forge fault) per call, noting the in-lock status refreshes so far."""

    def __init__(self, *results: ReviewThreadAudit | ForgeClientError) -> None:
        super().__init__(ReviewThreadAudit(supported=True, viewer_login=_VIEWER))
        self.results = list(results)
        self.status_refreshes_at_audit: list[int] = []
        self.audit_retries: list[bool] = []

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int, retry: bool = True
    ) -> ReviewThreadAudit:
        del repo, pr_number
        self.status_refreshes_at_audit.append(self.fetch_pr_status_calls)
        self.audit_retries.append(retry)
        result = self.results.pop(0)
        if isinstance(result, ForgeClientError):
            raise result
        return result


class _LockLog:
    """Merge coordinator + sleep double that logs whether each sleep holds the merge lock."""

    def __init__(self) -> None:
        self.held = False
        self.sleeps_under_lock: list[bool] = []

    @asynccontextmanager
    async def serialized_merge(self, *, repo_url: str, base_branch: str) -> AsyncIterator[None]:
        del repo_url, base_branch
        self.held = True
        try:
            yield
        finally:
            self.held = False

    async def sleep(self, seconds: float) -> None:
        del seconds
        self.sleeps_under_lock.append(self.held)


async def _merge_under_lock_log(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, gh: _MergeMethodClient
) -> tuple[bool | None, MonitorState, _LockLog, str]:
    workspace_id = await seed_monitoring_workspace(factory)
    gh.expect_context(
        repo=_TEST_REPO, pr_number=_TEST_PR_NUMBER, base_branch=_TEST_DEFAULT_BASE_BRANCH
    )
    lock = _LockLog()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=lock.sleep,  # type: ignore[arg-type]
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
        merge_coordinator=lock,
    )
    state = settled_baseline_state()
    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )
    return terminal, state, lock, workspace_id


async def test_feature_merge_reaudits_after_in_lock_status_refresh(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6vMb1w: a thread AWF's identity resolves silently while the
    monitor waits for the merge lock / pre-merge settle drops out of the in-lock
    status refresh, so ``decide`` still says Merge; the in-lock re-audit refuses."""
    gh = _SequencedAuditMergeClient(
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER),
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(_thread(_SILENT),)),
    )

    terminal, state, _, workspace_id = await _execute_merge(
        factory=factory, tmp_path=tmp_path, gh=gh
    )

    assert terminal is False
    assert gh.status_refreshes_at_audit == [0, 1]  # re-audit follows the in-lock refresh
    assert gh.merge_calls == []
    assert len(gh.comments) == 1
    assert "REVIEW_THREAD_DISPOSITION_UNVERIFIED" in gh.comments[0]
    assert _SILENT in gh.comments[0]
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]
    _, _, status = await _attention(factory, workspace_id)
    assert status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.parametrize("fresh_audit", [0, 1])
async def test_feature_merge_waits_for_a_poll_that_lists_feedback_opened_after_its_snapshot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    fresh_audit: int,
) -> None:
    """PRRT_kwDOSJAM6s6vVxR0: neither the pre-lock audit nor the in-lock re-audit merges
    over a thread opened after the status ``decide`` merged on."""
    clean = ReviewThreadAudit(supported=True, viewer_login=_VIEWER)
    fresh = ReviewThreadAudit(
        supported=True,
        viewer_login=_VIEWER,
        threads=(_thread(_FRESH, resolved=False, external=("P1",)),),
    )
    gh = _SequencedAuditMergeClient(*[clean] * fresh_audit, fresh)

    terminal, _, _, _ = await _execute_merge(factory=factory, tmp_path=tmp_path, gh=gh)

    assert terminal is False
    assert gh.status_refreshes_at_audit == [0, 1][: fresh_audit + 1]
    assert gh.merge_calls == []
    assert gh.comments == []


async def test_feature_merge_in_lock_reaudit_fails_fast(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6vbU2K: only the pre-lock audit retries in transport; the in-lock
    re-audit fails fast, so a transient blip never backs off holding the merge lock."""
    clean = ReviewThreadAudit(supported=True, viewer_login=_VIEWER)
    gh = _SequencedAuditMergeClient(clean, clean)

    terminal, _, _, _ = await _execute_merge(factory=factory, tmp_path=tmp_path, gh=gh)

    assert terminal is True
    assert gh.status_refreshes_at_audit == [0, 1]
    assert gh.audit_retries == [True, False]


async def test_feature_merge_in_lock_transient_audit_fault_backs_off_after_release(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The in-lock re-audit never sleeps holding the merge lock: it backs off after release."""
    gh = _SequencedAuditMergeClient(
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER),
        GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502"),
    )

    terminal, state, lock, _ = await _merge_under_lock_log(factory, tmp_path, gh)

    assert terminal is False
    assert gh.status_refreshes_at_audit == [0, 1]
    assert gh.merge_calls == []
    assert gh.comments == []
    assert lock.sleeps_under_lock == [False]
    assert (
        state.threads_addressed_ids["__awf_forge_transient_retry_count:review_thread_audit"] == "1"
    )


async def test_feature_merge_in_lock_permanent_audit_fault_notifies_unavailable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _SequencedAuditMergeClient(
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER),
        GitHubClientError(
            operation="gh api graphql", returncode=1, stderr="GraphQL: Could not resolve to a node"
        ),
    )

    terminal, _, lock, workspace_id = await _merge_under_lock_log(factory, tmp_path, gh)

    assert terminal is False
    assert gh.merge_calls == []
    assert len(gh.comments) == 1
    assert "REVIEW_THREAD_AUDIT_UNAVAILABLE" in gh.comments[0]
    assert True not in lock.sleeps_under_lock
    events = await _audit_events(factory, workspace_id)
    assert [(e["outcome"], e["reason_code"]) for e in events] == [
        ("failed", "REVIEW_THREAD_AUDIT_UNAVAILABLE")
    ]
