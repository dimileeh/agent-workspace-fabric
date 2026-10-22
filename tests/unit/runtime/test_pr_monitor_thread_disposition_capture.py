"""Disposition capture at verdict time (#1018).

The FALSE POSITIVE reason used to be discarded once the monitor narrowed the
agent's result to a bare verdict, so nothing could be published in the thread.
These tests pin where each disposition input is captured: the agent's reason
(FALSE POSITIVE / DEFER), the item's own fix commit (FIXED), and the DEFER
tracking issue — which no longer gets a swallowed top-level PR comment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _item_disposition_key
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
    _review_thread_body_hash,
)
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comment_repair_provenance import (
    ItemCommitProvenance,
    _write_item_commit_provenance_record,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AgentVerdictExecutionError,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.fix_cycle_deferred import (
    _capture_deferred_review_thread,
    _deferred_issue_filed_marker,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    read_item_disposition,
    record_item_disposition,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

pytestmark = pytest.mark.unit

_REPO = RepoRef(owner="acme", name="widgets")
_START = "a" * 40
_FIX = "b" * 40
_ISSUE = "https://github.com/acme/widgets/issues/7"


def _thread(thread_id: str) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path="widgets/core.py",
        line=12,
        body_excerpt="**P1** worker crashes when payload is None",
        author="review-bot",
    )


def _verdict_runner(
    *,
    result: VerdictResult | None = None,
    error: AgentVerdictExecutionError | None = None,
    worktrees_root: Path | None = None,
    heads: list[str] | None = None,
) -> SimpleNamespace:
    async def _resolve_task_tag(_workspace_id: str) -> str | None:
        return None

    async def _invoke(**_kwargs: object) -> VerdictResult:
        if error is not None:
            raise error
        assert result is not None
        return result

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return (heads or [None]).pop(0)

    return SimpleNamespace(
        _workspace_runtime_context="",
        _worktrees_root=worktrees_root,
        _deps=SimpleNamespace(session_factory=None),
        _resolve_task_tag=_resolve_task_tag,
        _invoke_cli_for_verdict_result=_invoke,
        _rev_parse_head=_rev_parse_head,
    )


async def _address(runner: SimpleNamespace, thread: ReviewThread, state: MonitorState) -> str:
    return await comments._address_thread(
        cast(Any, runner),
        workspace_id="ws_capture",
        repo=_REPO,
        pr_number=42,
        thread=thread,
        compose_project="proj",
        compose_file=Path("/tmp/compose.yml"),
        state=state,
        owned_paths=["widgets/"],
        task_tag=None,
        operation_start_head=_START,
        operation_id="op_capture",
    )


async def test_address_thread_records_false_positive_reason() -> None:
    state = MonitorState()
    reason = "the guard at widgets/core.py:12 already rejects None"
    runner = _verdict_runner(result=VerdictResult(verdict="false_positive", reason=reason))

    verdict = await _address(runner, _thread("PRRT_test_fp"), state)

    assert verdict == "false_positive"
    disposition = read_item_disposition(state, "PRRT_test_fp")
    assert disposition is not None
    assert disposition.disposition_class == "false_positive"
    assert disposition.verdict == "false_positive"
    assert disposition.reason == reason


async def test_address_thread_records_fixed_commit_from_provenance_chain(tmp_path: Path) -> None:
    (tmp_path / "ws_capture").mkdir()
    state = MonitorState()
    runner = _verdict_runner(
        result=VerdictResult(verdict="fix_committed", reason="guard None payloads"),
        worktrees_root=tmp_path,
        heads=[_FIX],
    )

    verdict = await _address(runner, _thread("PRRT_test_fix"), state)

    assert verdict == "fix_committed"
    disposition = read_item_disposition(state, "PRRT_test_fix")
    assert disposition is not None
    assert disposition.disposition_class == "fixed"
    assert disposition.commits == (_FIX,)
    assert disposition.commit_base == _START
    assert disposition.reason == "guard None payloads"


async def test_address_thread_agent_failed_clears_disposition() -> None:
    state = MonitorState()
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id="PRRT_test_af",
            disposition_class="false_positive",
            verdict="false_positive",
            reason="stale reason from an earlier pass",
        ),
    )
    runner = _verdict_runner(error=AgentVerdictExecutionError(reason_code="AGENT_TIMEOUT"))

    verdict = await _address(runner, _thread("PRRT_test_af"), state)

    assert verdict == "agent_failed"
    assert _item_disposition_key("PRRT_test_af") not in state.threads_addressed_ids


async def test_review_comment_verdict_records_disposition_for_promotion() -> None:
    state = MonitorState()
    runner = _verdict_runner(
        result=VerdictResult(verdict="false_positive", reason="already validated upstream")
    )
    comment = ReviewComment(
        comment_id="4688598838",
        body_excerpt="consider validating the payload",
        author="review-bot",
        state="COMMENTED",
    )

    result = await comments._address_review_comment_result(
        cast(Any, runner),
        workspace_id="ws_capture",
        repo=_REPO,
        pr_number=42,
        comment=comment,
        compose_project="proj",
        compose_file=Path("/tmp/compose.yml"),
        state=state,
        owned_paths=["widgets/"],
        task_tag=None,
        operation_start_head=_START,
    )

    assert result.verdict == "false_positive"
    disposition = read_item_disposition(state, "4688598838")
    assert disposition is not None
    assert disposition.reason == "already validated upstream"


class _DeferForge:
    def __init__(self) -> None:
        self.posted_comments: list[str] = []
        self.issues: list[tuple[str, str]] = []

    async def create_issue(self, *, repo: RepoRef, title: str, body: str) -> str:
        del repo
        self.issues.append((title, body))
        return _ISSUE

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        del repo, pr_number
        self.posted_comments.append(body)


def _defer_runner(gh: _DeferForge) -> SimpleNamespace:
    events: list[dict[str, object]] = []

    async def _record(**kwargs: object) -> None:
        events.append(kwargs)

    async def _clear(**_kwargs: object) -> None:
        return None

    return SimpleNamespace(
        _deps=SimpleNamespace(gh=gh),
        _record_pr_monitor_audit_event=_record,
        _clear_forge_transient_retry_state_on_success=_clear,
        events=events,
    )


async def _capture(runner: SimpleNamespace, thread: ReviewThread, state: MonitorState) -> object:
    return await _capture_deferred_review_thread(
        runner,
        workspace_id="ws_capture",
        repo=_REPO,
        pr_number=42,
        thread=thread,
        state=state,
        base_branch="development",
        remote_branch="awf/ws_capture",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )


async def test_deferred_capture_no_longer_posts_top_level_comment_and_records_issue_url() -> None:
    gh = _DeferForge()
    runner = _defer_runner(gh)
    state = MonitorState()
    thread = _thread("PRRT_test_defer")
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id="PRRT_test_defer",
            disposition_class="deferred",
            verdict="defer",
            reason="track the retry-budget follow-up",
        ),
    )

    captured = await _capture(runner, thread, state)

    assert captured is True
    assert gh.posted_comments == []
    assert len(gh.issues) == 1
    disposition = read_item_disposition(state, "PRRT_test_defer")
    assert disposition is not None
    assert disposition.issue_url == _ISSUE
    assert disposition.reason == "track the retry-budget follow-up"
    assert runner.events[-1]["outcome"] == "succeeded"


async def test_deferred_capture_already_filed_takes_issue_url_from_marker() -> None:
    gh = _DeferForge()
    runner = _defer_runner(gh)
    state = MonitorState()
    thread = _thread("PRRT_test_filed")
    state.mark_addressed(
        _deferred_issue_filed_marker(thread.thread_id, _review_thread_body_hash(thread)), _ISSUE
    )
    state.mark_addressed("__defer_reason__:PRRT_test_filed", "follow up later")

    captured = await _capture(runner, thread, state)

    assert captured is True
    assert gh.issues == []
    assert gh.posted_comments == []
    disposition = read_item_disposition(state, "PRRT_test_filed")
    assert disposition is not None
    assert disposition.issue_url == _ISSUE
    assert disposition.reason == "follow up later"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


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


async def test_fix_cycle_attaches_pending_fixed_commits_before_chain_clear(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktrees_root = tmp_path / "worktrees"
    (worktrees_root / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    state = MonitorState()

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (_START, None)

    async def _none(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return _FIX

    async def _address_thread(**kwargs: object) -> str:
        thread = cast(ReviewThread, kwargs["thread"])
        # The HEAD probe was unreadable at verdict time, so FIXED is still pending
        # its commit; the #935 pre-push settle then links the chain record.
        record_item_disposition(
            state,
            ThreadDisposition(
                item_id=thread.thread_id,
                disposition_class="fixed",
                verdict="fix_committed",
                reason="guard None payloads",
            ),
        )
        await _write_item_commit_provenance_record(
            runner,
            workspace_id=workspace_id,
            state=state,
            record=ItemCommitProvenance(
                item_id=thread.thread_id, item_start_head=_START, head_sha=_FIX
            ),
        )
        return "fix_committed"

    async def _settle(**_kwargs: object) -> PRStatus:
        return _status(_START)

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _settle)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _none)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        pr_head_sha=_START,
        initial_threads=(_thread("PRRT_test_pending"),),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    disposition = read_item_disposition(state, "PRRT_test_pending")
    assert disposition is not None
    assert disposition.commits == (_FIX,)
    assert disposition.commit_base == _START
