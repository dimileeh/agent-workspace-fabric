"""Focused branch-coverage tests for PR monitor runner edge behavior (split part)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.control.quality_gates import QualityGateViolation
from awf.db.repositories import (
    WorkspaceEventCreate,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import comments as pr_monitor_runner_comments
from awf.runtime.pr_monitor_runner.helpers import (
    _review_comment_body_state_key,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
    _ProtectedScopePushBlock,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorPolicyBlockedError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _status_for_helpers(
    *,
    head_sha: str = "abc1234567890def",
    threads: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    blocking_reviews: tuple[ReviewComment, ...] | None = None,
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=reviews,
        blocking_reviews=(
            tuple(review for review in reviews if review.blocks_merge)
            if blocking_reviews is None
            else blocking_reviews
        ),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


class _FailingLogSink:
    async def write(self, data: str) -> None:
        del data
        raise RuntimeError("log sink unavailable")


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


@pytest.mark.unit
async def test_fix_cycle_clears_addressed_thread_state_on_policy_blocked_review(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The review-comment loop's policy-blocked exit must also roll back the
    # thread already addressed earlier in the same cycle.
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fixed_thread = ReviewThread(
        thread_id="T_fixed", path="src/foo.py", line=12, body_excerpt="fix me first", author="rev"
    )
    blocked_comment = ReviewComment(comment_id="C_blocked", body_excerpt="policy blocks review")
    state = MonitorState()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _address_review(**_kwargs: object) -> object:
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked review fix.")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner, "_address_review_comment_result", _address_review)

    result = await runner._run_fix_cycle(
        workspace_id="ws_policy_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(fixed_thread,),
        initial_reviews=(blocked_comment,),
        state=state,
        remote_branch="awf/ws_policy_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert "Supply-chain policy blocked" in result.stderr
    assert "T_fixed" not in state.threads_addressed_ids


@pytest.mark.unit
async def test_fix_cycle_pauses_into_blocked_and_preserves_protected_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS-2: a protected-scope violation in an unpushed fix-cycle commit pauses
    the workspace into ``blocked`` (NOT failed), PRESERVES the offending commit
    (no ``git reset --hard``), records the preserved HEAD, and posts a PR
    notification comment — instead of the old silent rollback."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="start-sha\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # preserved HEAD
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    thread = ReviewThread(
        thread_id="T_protected",
        path="tests/unit/control/test_ci_workflow_toolchain.py",
        line=49,
        body_excerpt="this test requires a protected workflow edit",
        author="reviewer",
    )
    state = MonitorState()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _fetch_clean_status(**_kwargs: object) -> PRStatus:
        return _status_for_helpers()

    async def _protected_block(**_kwargs: object) -> _ProtectedScopePushBlock:
        return _ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                ),
            ),
        )

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a paused workspace must not push")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _fetch_clean_status, raising=False)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_block)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="start-sha",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.paused_into_blocked is True
    assert result.reason_code == "PROTECTED_SCOPE_PAUSED_BLOCKED"
    assert result.details is not None
    assert result.details["preserved_head_sha"] == "blocked-head-sha"
    # The offending commit is PRESERVED — no reset/clean before the operator decides.
    assert _git_worktree_command(worktree, "reset", "--hard", "start-sha") not in [
        call.args for call in cmd.calls
    ]
    assert not any(call.args[5:7] == ["reset", "--hard"] for call in cmd.calls)
    # An operator notification comment was posted to the PR.
    assert len(gh.posts) == 1

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == "blocked"
        assert workspace.block_epoch == 1
        assert workspace.block_resume_phase == "monitor_protected_scope_push"
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_paused",
            limit=10,
        )
    assert any(
        event.payload and event.payload.get("preserved_head_sha") == "blocked-head-sha"
        for event in events
    )


@pytest.mark.unit
async def test_protected_pause_notification_forge_failure_does_not_strand_block(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS-2: after the ``monitoring_pr -> blocked`` commit, a transient/permission
    forge fault on the best-effort PR notification comment MUST NOT escape. If it
    did, the caller would never reach its ``paused_into_blocked`` branch, leaving
    the monitor operation running and state markers unpersisted while the row is
    already blocked. The pause result must still come back ``paused_into_blocked``
    and the notification dedupe marker must stay unset so a resume re-notifies."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="start-sha\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # preserved HEAD
    gh = _FailingPostGh()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    thread = ReviewThread(
        thread_id="T_protected",
        path="tests/unit/control/test_ci_workflow_toolchain.py",
        line=49,
        body_excerpt="this test requires a protected workflow edit",
        author="reviewer",
    )
    state = MonitorState()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _fetch_clean_status(**_kwargs: object) -> PRStatus:
        return _status_for_helpers()

    async def _protected_block(**_kwargs: object) -> _ProtectedScopePushBlock:
        return _ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                ),
            ),
        )

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a paused workspace must not push")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _fetch_clean_status, raising=False)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_block)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    # The forge fault on the notification comment must not propagate out.
    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="start-sha",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.paused_into_blocked is True
    assert result.failed is True
    # The notification was attempted but failed; the dedupe marker stays unset so a
    # later resume re-notifies rather than silently suppressing the comment.
    assert gh.attempts == 1
    assert not any(key.startswith("__awf_protected_block__") for key in state.threads_addressed_ids)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == "blocked"


@pytest.mark.unit
async def test_fix_cycle_returns_failed_push_when_review_fix_hits_policy_block(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    review = ReviewComment(
        comment_id="R_supply",
        body_excerpt="please adjust this",
        author="reviewer",
    )

    async def _blocked_review(**_kwargs: object) -> str:
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked review fix.")

    monkeypatch.setattr(runner, "_address_review_comment_result", _blocked_review)

    result = await runner._run_fix_cycle(
        workspace_id="ws_supply_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(review,),
        state=MonitorState(),
        remote_branch="awf/ws_supply_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 1
    assert "Supply-chain policy blocked review fix" in result.stderr


@pytest.mark.unit
async def test_fix_cycle_clears_addressed_review_state_on_protected_scope_early_return(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fixed_review = ReviewComment(
        comment_id="C_fixed",
        body_excerpt="please adjust this first",
        author="reviewer",
    )
    blocked_review = ReviewComment(
        comment_id="C_blocked",
        body_excerpt="then protected scope diff fails",
        author="reviewer",
    )
    state = MonitorState()

    async def _address_review_comment_result(
        **kwargs: object,
    ) -> pr_monitor_runner_comments.VerdictResult:
        comment = kwargs["comment"]
        assert isinstance(comment, ReviewComment)
        if comment.comment_id == fixed_review.comment_id:
            return pr_monitor_runner_comments.VerdictResult(verdict="fix_committed")
        raise ProtectedScopeDiffError("diff baseline unavailable")

    async def _protected_scope_result(**kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=str(kwargs["exc"]),
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        )

    monkeypatch.setattr(
        runner,
        "_address_review_comment_result",
        _address_review_comment_result,
    )
    monkeypatch.setattr(
        runner,
        "_protected_scope_diff_unavailable_push_result",
        _protected_scope_result,
    )

    result = await runner._run_fix_cycle(
        workspace_id="ws_protected_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(fixed_review, blocked_review),
        state=state,
        remote_branch="awf/ws_protected_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert "C_fixed" not in state.threads_addressed_ids
    assert _review_comment_body_state_key("C_fixed") not in state.threads_addressed_ids


@pytest.mark.unit
async def test_fix_cycle_zero_passes_still_runs_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    object.__setattr__(runner._runner_config, "max_fix_cycle_passes", 0)

    await runner._run_fix_cycle(
        workspace_id="ws_zero_pass",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_zero_pass",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:5] == _git_worktree_command(tmp_path / "worktrees" / "ws_zero_pass")
    assert cmd.calls[0].args[5] == "push"


@pytest.mark.unit
async def test_best_effort_monitor_log_and_missing_workspace_event_append_do_not_raise(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._write_monitor_log(_FailingLogSink(), {"event": "monitor.test"})  # type: ignore[arg-type]
    await runner._append_workspace_events(
        workspace_id="ws_missing",
        events=[
            WorkspaceEventCreate(
                event_type="workspace.test",
                reason_code="TEST",
                payload={"ok": True},
            )
        ],
    )


@pytest.mark.unit
async def test_post_human_notification_dedup_skips_github_call(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    status = _status_for_helpers()
    state = MonitorState(
        threads_addressed_ids={"__awf_notify__:abc1234567890def:manual": "notified"}
    )

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=status,
        state=state,
        blocker_reason="manual",
    )

    assert cmd.calls == []


class _RecordingGh:
    """Minimal gh double that records ``post_comment`` invocations."""

    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    async def post_comment(self, *, repo: object, pr_number: int, body: str) -> None:
        self.posts.append({"repo": repo, "pr_number": pr_number, "body": body})


class _FailingPostGh:
    """gh double whose ``post_comment`` raises a forge fault (transient/permission)."""

    def __init__(self) -> None:
        self.attempts = 0

    async def post_comment(self, *, repo: object, pr_number: int, body: str) -> None:
        self.attempts += 1
        raise ForgeClientError("forge unavailable")


@pytest.mark.unit
async def test_post_human_notification_dedupes_on_explicit_notification_key(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An explicit ``notification_key`` keys the dedupe on block epoch/content,
    not head/reason: re-posting under the SAME key is suppressed, but a DIFFERENT
    key (a new epoch / changed violation) re-notifies even on the same head."""
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    status = _status_for_helpers()
    state = MonitorState()
    repo = RepoRef(owner="example", name="repo")

    # First post under epoch-1 key.
    await runner._post_human_notification_once(
        repo=repo,
        pr_number=42,
        status=status,
        state=state,
        blocker_reason="protected file pyproject.toml changed",
        notification_key="__awf_protected_block__:1:digestA",
    )
    assert len(gh.posts) == 1

    # Same key → suppressed (idempotent re-notify within one block epoch).
    await runner._post_human_notification_once(
        repo=repo,
        pr_number=42,
        status=status,
        state=state,
        blocker_reason="protected file pyproject.toml changed",
        notification_key="__awf_protected_block__:1:digestA",
    )
    assert len(gh.posts) == 1

    # Different key (new epoch / changed violation) on the SAME head → re-notifies.
    await runner._post_human_notification_once(
        repo=repo,
        pr_number=42,
        status=status,
        state=state,
        blocker_reason="protected file .coveragerc changed",
        notification_key="__awf_protected_block__:2:digestB",
    )
    assert len(gh.posts) == 2


@pytest.mark.unit
def test_protected_block_notification_key_is_stable_per_epoch_and_content() -> None:
    """The protected-block key changes with epoch or violation content so a second
    different violation is not suppressed by the once-per-lifetime dedupe."""
    from awf.runtime.pr_monitor_runner.helpers import (
        _protected_block_notification_key,
        _protected_block_violations_digest,
    )

    v1 = (
        QualityGateViolation(
            path="pyproject.toml",
            protected_pattern="pyproject.toml",
            section="pyproject.toml",
            line=None,
            reason="weakened coverage",
        ),
    )
    v2 = (
        QualityGateViolation(
            path=".coveragerc",
            protected_pattern=".coveragerc",
            section=".coveragerc",
            line=None,
            reason="weakened coverage",
        ),
    )
    digest1 = _protected_block_violations_digest(v1)
    # Order-independent: same violations in any order produce the same digest.
    assert _protected_block_violations_digest(tuple(reversed(v1 + v1))) == (
        _protected_block_violations_digest(v1 + v1)
    )
    key_epoch1 = _protected_block_notification_key(block_epoch=1, violations_digest=digest1)
    key_epoch2 = _protected_block_notification_key(block_epoch=2, violations_digest=digest1)
    key_v2 = _protected_block_notification_key(
        block_epoch=1, violations_digest=_protected_block_violations_digest(v2)
    )
    assert key_epoch1 != key_epoch2  # new epoch → new key
    assert key_epoch1 != key_v2  # different content → new key
    assert key_epoch1.startswith("__awf_protected_block__:")


@pytest.mark.unit
async def test_defer_signal_write_failure_is_best_effort(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    artifact_file = tmp_path / "artifacts-file"
    artifact_file.write_text("not a directory", encoding="utf-8")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifact_file,
    )

    runner._write_defer_signal(
        workspace_id="ws_defer",
        pr_number=42,
        terminal_action="Abort",
        merged=False,
        status=_status_for_helpers(),
        state=MonitorState(),
    )

    assert artifact_file.read_text(encoding="utf-8") == "not a directory"


@pytest.mark.unit
async def test_target_branch_reconcile_failure_appends_workspace_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    async def failing_reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        assert repo_url == "git@github.com:dimileeh/aira-web.git"
        assert branch == "development"
        raise RuntimeError("target branch locked")

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=failing_reconciler,
    )

    with structlog.testing.capture_logs() as captured:
        await runner._reconcile_target_branch_after_merge(
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            base_branch="development",
        )

    failure_log = next(
        event
        for event in captured
        if event.get("event") == "monitor.target_branch_reconcile_failed"
    )
    assert failure_log["status"] == "failed"
    assert failure_log["reason_code"] == "TARGET_BRANCH_RECONCILE_FAILED"
    assert failure_log["error_type"] == "RuntimeError"
    assert failure_log["resolver_results"] == []
    assert failure_log["commit_sha"] is None
    assert failure_log["pushed"] is False
    assert failure_log["dry_run"] is None
    assert failure_log["commit_allowed"] is None

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.events[-1].event_type == "target_branch.reconcile_failed"
        assert ws.events[-1].reason_code == "TARGET_BRANCH_RECONCILE_FAILED"
        assert ws.events[-1].payload["error"] == "target branch locked"
