"""Unit tests for focused ``pr_monitor_runner`` behavior.

Most cases cover the pure, side-effect-free helpers: ``_parse_verdict`` (CLI
reply → structured verdict) and ``_collect_defer_items`` (PRStatus +
MonitorState → bot/human defer buckets for the terminal artifact). Focused
runtime-path regressions live here when the unit suite needs to cover a
specific merge-gate branch without running the full monitor integration loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.base import Base
from awf.db.enums import OperationStatus, TaskClass, WorkspaceStatus
from awf.db.models import ADVISORY_PLAN_ARTIFACT_OVERLAP_REASON
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    StaleReasonCreate,
    StaleReasonRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.db.session import make_engine, make_session_factory
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    CheckTiming,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PullRequestMonitorRunner,
    _as_utc,
    _collect_defer_items,
    _infer_service_work_dir,
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _initial_review_grace_state_for_persistence,
    _initial_review_grace_state_for_runtime,
    _initial_review_grace_wait_seconds,
    _initial_review_grace_wall_seconds,
    _initial_review_grace_wall_started_value_from_datetime,
    _is_pending_check,
    _merge_rejection_reason,
    _non_check_reviewer_settle_started_key,
    _notify_human_reason,
    _parse_verdict,
    _stale_pending_check_warning_key,
    _stale_pending_check_warnings,
    _target_reconcile_payload,
    _with_ci_failures,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor import _status


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'monitor.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _monitor_runner(tmp_path: Path, fake: FakeCommandRunner) -> PullRequestMonitorRunner:
    return PullRequestMonitorRunner(
        session_factory=object(),  # type: ignore[arg-type]
        runner=fake,
        adapter=object(),  # type: ignore[arg-type]
        gh=object(),  # type: ignore[arg-type]
        worktrees_root=tmp_path / "work" / "git" / "worktrees",
    )


def _green_status(*, pr_number: int = 42, head_sha: str = "abc1234567890def") -> PRStatus:
    return PRStatus(
        number=pr_number,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


def _gh_pr_merge_calls(cmd: FakeCommandRunner) -> list[list[str]]:
    return [call.args for call in cmd.calls if call.args[:3] == ["gh", "pr", "merge"]]


async def _mark_refactor_task(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    auto_merge: bool = True,
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.task_class = TaskClass.refactor_task.value
        workspace.auto_merge = auto_merge
        await session.commit()


async def _dispatch_merge_recovery(
    *,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    workspace_id: str,
    pr_number: int,
    head_sha: str,
    sleep_fn: RecordedSleep | None = None,
) -> bool:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn or RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    return await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )


@pytest.mark.unit
async def test_advisory_plan_artifact_stale_reason_does_not_dispatch_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        assert candidate is not None
        await StaleReasonRepository(session).replace_active_findings(
            workspace_id=workspace_id,
            candidate_id=candidate.id,
            attempt_id=candidate.attempt_id,
            task_id=candidate.task_id,
            findings=[
                StaleReasonCreate(
                    reason_code=ADVISORY_PLAN_ARTIFACT_OVERLAP_REASON,
                    trigger_type="path_overlap",
                    trigger_ref="docs/awf-plans/ws_other.md",
                    explanation=(
                        "Target branch changed another workspace's AWF plan artifact."
                    ),
                )
            ],
        )
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    gate = await runner._merge_gate_for_workspace(workspace_id)
    handled = await runner._handle_merge_gate_blocker(
        gate=gate,
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        assert candidate is not None
        stale_reasons = await StaleReasonRepository(session).list_active_for_candidate(
            candidate.id
        )
        operations = await OperationRepository(session).list_all(
            workspace_id=workspace_id
        )

    assert gate.stale_reason is None
    assert gate.req_action is None
    assert handled is None
    assert candidate.stale is False
    assert candidate.stale_reason is None
    assert [(r.reason_code, r.blocks_merge, r.severity) for r in stale_reasons] == [
        (ADVISORY_PLAN_ARTIFACT_OVERLAP_REASON, False, "advisory")
    ]
    assert operations == []
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_auto_merge_waits_for_initial_review_grace_before_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_green_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert state.threads_addressed_ids[_initial_review_grace_started_key(42)]
    assert _gh_pr_merge_calls(cmd) == []
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_auto_merge_dispatches_validation_recovery_before_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 81
    head_sha = "b" * 40
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert terminal is True
    assert _gh_pr_merge_calls(cmd) == []
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert len(operations) == 1
    assert operations[0].type == "validate"
    assert operations[0].status == OperationStatus.pending.value
    assert operations[0].payload["action"] == "validate_only"
    assert operations[0].payload["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"
    assert operations[0].payload["source_head_sha"] == head_sha


@pytest.mark.unit
async def test_auto_merge_dispatches_active_stale_recovery_before_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 82
    head_sha = "c" * 40
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    async with factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        assert candidate is not None
        await StaleReasonRepository(session).replace_active_findings(
            workspace_id=workspace_id,
            candidate_id=candidate.id,
            attempt_id=candidate.attempt_id,
            task_id=candidate.task_id,
            findings=[
                StaleReasonCreate(
                    reason_code="STALE_TARGET_ADVANCED",
                    trigger_type="target_advanced",
                    trigger_ref="d" * 40,
                    explanation="Target branch advanced past this candidate.",
                )
            ],
        )
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert terminal is True
    assert _gh_pr_merge_calls(cmd) == []
    assert candidate is not None
    assert candidate.stale is True
    assert candidate.stale_reason == "STALE_TARGET_ADVANCED"
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert len(operations) == 1
    assert operations[0].payload["action"] == "rebase_only"
    assert operations[0].payload["reason_code"] == "STALE_TARGET_ADVANCED"


@pytest.mark.unit
async def test_auto_merge_waits_for_non_check_reviewer_settle_before_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 83
    head_sha = "head-without-visible-reviewer"
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )
    state = MonitorState(started_at=0.0)

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert (
        state.threads_addressed_ids[
            _non_check_reviewer_settle_started_key(
                pr_number=pr_number,
                head_sha=head_sha,
            )
        ]
    )
    assert _gh_pr_merge_calls(cmd) == []
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_pre_merge_recheck_blocks_when_check_becomes_pending(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # git fetch origin development
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(check_state="PENDING"))
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        pre_merge_settle_seconds=5,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_green_status(),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)

    assert terminal is False
    assert sleep_fn.calls == [5, 60]
    assert _gh_pr_merge_calls(cmd) == []
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_clean_pr_merges_only_after_pre_merge_recheck_passes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # git fetch origin development
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload())  # final clean PR snapshot
    cmd.queue_result(returncode=0)  # gh pr merge
    cmd.queue_result(returncode=0, stdout="MERGESHA\n")  # merge commit lookup
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
        pre_merge_settle_seconds=5,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )
    state = MonitorState(
        started_at=0.0,
        threads_addressed_ids={_initial_review_grace_done_key(42): "elapsed"},
    )
    status = replace(
        _green_status(),
        checks=(CheckTiming(name="Greptile", status="COMPLETED", conclusion="SUCCESS"),),
    )

    terminal = await runner._execute(
        action=Merge(),
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

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
        assert attempt is not None
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt.id)

    graphql_index = next(
        index for index, call in enumerate(cmd.calls) if call.args[:3] == ["gh", "api", "graphql"]
    )
    merge_index = next(
        index for index, call in enumerate(cmd.calls) if call.args[:3] == ["gh", "pr", "merge"]
    )
    assert terminal is True
    assert sleep_fn.calls == [5]
    assert graphql_index < merge_index
    assert len(_gh_pr_merge_calls(cmd)) == 1
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.completed.value
    assert workspace.pr_merge_sha == "MERGESHA"
    assert candidate is not None
    assert candidate.status == "merged"


class TestParseVerdict:
    @pytest.mark.unit
    def test_empty_stdout_defers(self) -> None:
        assert _parse_verdict("") == "defer"

    @pytest.mark.unit
    def test_false_positive_marker(self) -> None:
        assert _parse_verdict("FALSE POSITIVE: reviewer misread the diff") == "false_positive"

    @pytest.mark.unit
    def test_false_positive_case_insensitive(self) -> None:
        assert _parse_verdict("false positive: minor") == "false_positive"

    @pytest.mark.unit
    def test_defer_marker(self) -> None:
        assert _parse_verdict("DEFER: needs human judgement") == "defer"

    @pytest.mark.unit
    def test_plain_reply_counts_as_fix_committed(self) -> None:
        assert _parse_verdict("Committed fix in abc1234: renamed variable.") == "fix_committed"

    @pytest.mark.unit
    def test_false_positive_takes_precedence_over_defer(self) -> None:
        # Scanner checks FALSE POSITIVE first.
        reply = "FALSE POSITIVE: not a real issue. (not DEFER:)"
        assert _parse_verdict(reply) == "false_positive"


class TestCollectDeferItems:
    @pytest.mark.unit
    def test_empty_status_yields_empty_buckets(self) -> None:
        bots, humans = _collect_defer_items(_status(), MonitorState())
        assert bots == []
        assert humans == []

    @pytest.mark.unit
    def test_thread_deferred_by_bot_goes_to_bot_bucket(self) -> None:
        t = ReviewThread(
            thread_id="T1",
            path="src/x.py",
            line=1,
            body_excerpt="nit",
            author="coderabbitai[bot]",
        )
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        bots, humans = _collect_defer_items(_status(inline=(t,)), state)
        assert len(bots) == 1
        assert bots[0]["id"] == "T1"
        assert bots[0]["kind"] == "thread"
        assert humans == []

    @pytest.mark.unit
    def test_thread_deferred_by_human_goes_to_human_bucket(self) -> None:
        t = ReviewThread(
            thread_id="T2",
            path="src/y.py",
            line=5,
            body_excerpt="real concern",
            author="dimileeh",
        )
        state = MonitorState(threads_addressed_ids={"T2": "defer"})
        bots, humans = _collect_defer_items(_status(inline=(t,)), state)
        assert bots == []
        assert len(humans) == 1
        assert humans[0]["id"] == "T2"

    @pytest.mark.unit
    def test_non_deferred_items_are_excluded(self) -> None:
        t = ReviewThread(
            thread_id="T3",
            path=None,
            line=None,
            body_excerpt="fixed",
            author="coderabbitai[bot]",
        )
        state = MonitorState(threads_addressed_ids={"T3": "fix_committed"})
        bots, humans = _collect_defer_items(_status(inline=(t,)), state)
        assert bots == []
        assert humans == []

    @pytest.mark.unit
    def test_non_deferred_review_comments_are_excluded(self) -> None:
        c = ReviewComment(
            comment_id="C2",
            body_excerpt="already handled",
            author="dimileeh",
        )

        bots, humans = _collect_defer_items(_status(reviews=(c,)), MonitorState())

        assert bots == []
        assert humans == []

    @pytest.mark.unit
    def test_review_comment_deferred_includes_kind_review(self) -> None:
        c = ReviewComment(
            comment_id="C1",
            body_excerpt="overall concern",
            author="greptile-apps[bot]",
        )
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        bots, humans = _collect_defer_items(_status(reviews=(c,)), state)
        assert len(bots) == 1
        assert bots[0]["kind"] == "review"
        assert bots[0]["id"] == "C1"
        assert humans == []


class TestRunnerConfigShape:
    @pytest.mark.unit
    def test_runner_config_defaults_include_safety_net(self) -> None:
        """The runner keeps ``max_outer_iterations`` as a pure safety net
        against decision-loop bugs — a legitimate session exits via a
        terminal action well before this. The cap that WAS removed is
        ``MonitorConfig.iter_cap`` (decision-core gate). Keep these
        distinct so future refactors don't conflate them."""
        cfg = MonitorRunnerConfig()
        assert cfg.max_outer_iterations >= 1000
        assert cfg.max_fix_cycle_passes >= 1


class TestPendingCheckHelpers:
    @pytest.mark.unit
    def test_pending_check_warnings_include_only_old_non_terminal_checks(self) -> None:
        now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
        old = now - timedelta(minutes=10)
        status = replace(
            _status(),
            checks=(
                CheckTiming(
                    name="ci/build",
                    status="IN_PROGRESS",
                    started_at=old,
                    details_url="https://checks.example/build",
                ),
                CheckTiming(name="ci/no-start", status="PENDING", started_at=None),
                CheckTiming(name="ci/fresh", status="QUEUED", started_at=now),
                CheckTiming(name="ci/done", status="COMPLETED", conclusion=None, started_at=old),
                CheckTiming(name="ci/skipped", status=None, conclusion="SKIPPED", started_at=old),
            ),
        )

        disabled = _stale_pending_check_warnings(
            status,
            now=now,
            threshold_seconds=0,
        )
        warnings = _stale_pending_check_warnings(
            status,
            now=now,
            threshold_seconds=120,
        )

        assert disabled == ()
        assert len(warnings) == 1
        assert warnings[0].payload() == {
            "check_name": "ci/build",
            "age_seconds": 600,
            "head_sha": "abc123",
            "pr_number": 42,
            "threshold_seconds": 120,
            "threshold_window": 5,
            "check_status": "IN_PROGRESS",
            "check_conclusion": None,
            "details_url": "https://checks.example/build",
        }
        assert _stale_pending_check_warning_key(
            workspace_id="ws_1",
            head_sha="abc123",
            check_name="ci/build",
            threshold_seconds=120,
            threshold_window=5,
        ) == '__awf_pending_check_stale__:["ws_1","abc123","ci/build","120",5]'

    @pytest.mark.unit
    def test_pending_check_classifier_handles_provider_status_edges(self) -> None:
        assert _is_pending_check(CheckTiming(name="unknown", status="waiting")) is True
        assert _is_pending_check(CheckTiming(name="terminal", status="success")) is False
        assert (
            _is_pending_check(CheckTiming(name="terminal-conclusion", conclusion="timed_out"))
            is False
        )
        assert _is_pending_check(CheckTiming(name="future-provider", status="mystery")) is True
        assert _is_pending_check(CheckTiming(name="empty")) is False
        naive = datetime(2026, 4, 27, 12, 0)
        assert _as_utc(naive).tzinfo is UTC


class TestNotificationAndGraceHelpers:
    @pytest.mark.unit
    def test_notify_human_reason_prioritizes_blocking_conditions(self) -> None:
        blocking_review = ReviewComment(
            comment_id="C-block",
            body_excerpt="review skipped",
            author="review-bot",
            blocks_merge=True,
        )
        deferred_review = ReviewComment(
            comment_id="C-human",
            body_excerpt="please inspect",
            author="human",
        )
        deferred_state = MonitorState(threads_addressed_ids={"C-human": "defer"})

        assert "review was skipped" in (
            _notify_human_reason(_status(reviews=(blocking_review,)), MonitorState()) or ""
        )
        assert "required protection" in (
            _notify_human_reason(
                _status(merge_state_status=MergeStateStatus.BLOCKED),
                MonitorState(),
            )
            or ""
        )
        assert _notify_human_reason(
            _status(reviews=(deferred_review,)),
            deferred_state,
        ) == "human review feedback was deferred by the agent and remains unresolved"
        assert _notify_human_reason(_status(), MonitorState()) is None

    @pytest.mark.unit
    def test_initial_review_grace_state_converts_between_wall_and_monotonic_time(self) -> None:
        pr_number = 42
        started_key = _initial_review_grace_started_key(pr_number)
        done_key = _initial_review_grace_done_key(pr_number)
        wall_started = datetime(2026, 4, 27, 12, 0, tzinfo=UTC).timestamp()
        runtime_state = {started_key: f"{wall_started:.6f}"}
        persisted_state = {started_key: "900.000000"}

        assert _initial_review_grace_wall_seconds(object()) is None
        assert _initial_review_grace_wall_seconds("not-a-number") is None
        assert _initial_review_grace_wall_seconds("123.0") is None
        assert _initial_review_grace_wall_seconds(wall_started) == wall_started
        assert _initial_review_grace_wall_started_value_from_datetime(
            datetime(2026, 4, 27, 12, 0),
        ) == f"{wall_started:.6f}"

        converted_runtime = _initial_review_grace_state_for_runtime(
            runtime_state,
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started + 30.0,
        )
        legacy_runtime = _initial_review_grace_state_for_runtime(
            {started_key: "900.0"},
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started,
            legacy_monotonic_fallback=875.0,
        )
        converted_persistence = _initial_review_grace_state_for_persistence(
            persisted_state,
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started + 100.0,
        )
        invalid_persistence = _initial_review_grace_state_for_persistence(
            {started_key: "invalid"},
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started,
        )
        unchanged_persistence = _initial_review_grace_state_for_persistence(
            {},
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started,
        )

        assert converted_runtime[started_key] == "970.000000"
        assert legacy_runtime[started_key] == "875.000000"
        assert converted_persistence[started_key] == f"{wall_started:.6f}"
        assert invalid_persistence[started_key] == "invalid"
        assert unchanged_persistence == {}

        waiting = MonitorState(started_at=10.0)
        assert _initial_review_grace_wait_seconds(
            waiting,
            pr_number=pr_number,
            now=12.0,
            grace_seconds=10.0,
            poll_interval_seconds=3.0,
        ) == 3.0
        assert waiting.threads_addressed_ids[started_key] == "10.000000"

        invalid_started = MonitorState(
            started_at=20.0,
            threads_addressed_ids={started_key: "not-float"},
        )
        assert _initial_review_grace_wait_seconds(
            invalid_started,
            pr_number=pr_number,
            now=35.0,
            grace_seconds=10.0,
            poll_interval_seconds=5.0,
        ) == 0.0
        assert invalid_started.threads_addressed_ids[started_key] == "20.000000"
        assert invalid_started.threads_addressed_ids[done_key] == "elapsed"


class TestMiscMonitorHelpers:
    @pytest.mark.unit
    def test_merge_rejection_reason_and_service_work_dir_edges(self) -> None:
        assert _merge_rejection_reason("") == "GitHub rejected the merge attempt"
        assert _merge_rejection_reason(" ! [rejected] main -> main ") == (
            "GitHub rejected the merge attempt: ! [rejected] main -> main"
        )
        assert _infer_service_work_dir(Path("/srv/awf/git/worktrees")) == Path("/srv/awf")
        assert _infer_service_work_dir(Path("/srv/awf/worktrees")) == Path("/srv/awf")

    @pytest.mark.unit
    def test_target_reconcile_payload_accepts_dict_to_dict_and_fallback_objects(self) -> None:
        class _DictResult:
            def to_dict(self) -> dict[str, object]:
                return {"status": "clean"}

        class _BadDictResult:
            def to_dict(self) -> str:
                return "not a dict"

            def __str__(self) -> str:
                return "bad dict result"

        assert _target_reconcile_payload({"status": "updated"}) == {"status": "updated"}
        assert _target_reconcile_payload(_DictResult()) == {"status": "clean"}
        assert _target_reconcile_payload(_BadDictResult()) == {"result": "bad dict result"}
        assert _target_reconcile_payload(SimpleNamespace(status="unknown")) == {
            "result": "namespace(status='unknown')"
        }

    @pytest.mark.unit
    def test_ci_failure_replacement_preserves_status_shape(self) -> None:
        failure = CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom")
        updated = _with_ci_failures(_status(), (failure,))

        assert updated.ci_failures == (failure,)
        assert updated.head_sha == "abc123"

    @pytest.mark.unit
    async def test_write_monitor_log_swallows_sink_failures(
        self,
        tmp_path: Path,
    ) -> None:
        class _FailingSink:
            async def write(self, _payload: str) -> None:
                raise OSError("disk full")

        runner = _monitor_runner(tmp_path, FakeCommandRunner())

        await runner._write_monitor_log(_FailingSink(), {"event": "test"})  # type: ignore[arg-type]

    @pytest.mark.unit
    async def test_commit_dirty_worktree_branches(
        self,
        tmp_path: Path,
    ) -> None:
        async def run_case(
            workspace_id: str,
            queued: list[dict[str, object]],
            *,
            make_worktree: bool = True,
        ) -> bool:
            fake = FakeCommandRunner()
            for result in queued:
                fake.queue_result(**result)
            runner = _monitor_runner(tmp_path, fake)
            worktree = runner._worktrees_root / workspace_id
            if make_worktree:
                worktree.mkdir(parents=True, exist_ok=True)
            return await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message="awf: monitor dirty worktree",
            )

        assert await run_case("ws_missing", [], make_worktree=False) is False
        assert await run_case(
            "ws_status_failed",
            [{"returncode": 1, "stderr": "not a git repo"}],
        ) is False
        assert await run_case(
            "ws_clean",
            [{"returncode": 0, "stdout": ""}],
        ) is False
        assert await run_case(
            "ws_add_failed",
            [
                {"returncode": 0, "stdout": " M file.py\n"},
                {"returncode": 1, "stderr": "add failed"},
            ],
        ) is False
        assert await run_case(
            "ws_cached_clean",
            [
                {"returncode": 0, "stdout": " M file.py\n"},
                {"returncode": 0},
                {"returncode": 0},
            ],
        ) is False
        assert await run_case(
            "ws_commit_failed",
            [
                {"returncode": 0, "stdout": " M file.py\n"},
                {"returncode": 0},
                {"returncode": 1},
                {"returncode": 1, "stderr": "commit failed"},
            ],
        ) is False
        assert await run_case(
            "ws_committed",
            [
                {"returncode": 0, "stdout": " M file.py\n"},
                {"returncode": 0},
                {"returncode": 1},
                {"returncode": 0},
            ],
        ) is True


@pytest.mark.unit
async def test_monitor_recovery_dispatch_records_operation_with_pr_and_sha_context(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 77
    head_sha = "d" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)

    terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )

    assert terminal is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert workspace.status == WorkspaceStatus.ready.value
    assert len(operations) == 1
    operation = operations[0]
    assert operation.type == "validate"
    assert operation.status == OperationStatus.pending.value
    assert operation.idempotency_key is not None
    assert operation.idempotency_key.startswith("pr_monitor:validate_only:")
    assert len(operation.idempotency_key) <= 128
    assert operation.payload == {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": "validate_only",
        "requested_action": "validate",
        "reason": "Required validation tier has not passed for this merge candidate.",
        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        "stale_reason": "validation_insufficient_tier",
        "recovery_mode": "validate_only",
        "pr_number": pr_number,
        "pr_url": f"https://github.com/dimileeh/aira-web/pull/{pr_number}",
        "source_head_sha": head_sha,
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
    }


@pytest.mark.unit
async def test_validation_recovery_dispatch_is_idempotent_for_duplicate_tick_replay(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 78
    head_sha = "e" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)

    first_terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    replay_terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )

    assert first_terminal is True
    assert replay_terminal is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
        recovery_events = [
            event
            for event in workspace.events
            if event.event_type == "monitor.recovery_dispatched"
        ]
    assert len(operations) == 1
    assert operations[0].idempotency_key is not None
    assert operations[0].idempotency_key.startswith("pr_monitor:validate_only:")
    assert len(operations[0].idempotency_key) <= 128
    assert len(recovery_events) == 1


@pytest.mark.unit
async def test_manual_human_wait_records_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    sleep_fn = RecordedSleep()
    pr_number = 79
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
        auto_merge=False,
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    terminal = await runner._execute(
        action=NotifyHuman(message="manual merge required"),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert len(operations) == 1
    operation = operations[0]
    assert operation.type == "human_wait"
    assert operation.status == OperationStatus.succeeded.value
    assert operation.started_at is not None
    assert operation.finished_at is not None
    assert operation.payload == {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": "human_wait",
        "requested_action": "notify_human",
        "reason": "manual merge required",
        "reason_code": "HUMAN_WAIT",
        "pr_number": pr_number,
        "pr_url": f"https://github.com/dimileeh/aira-web/pull/{pr_number}",
        "source_head_sha": head_sha,
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
    }
    assert operation.result == {
        "status": "succeeded",
        "outcome": "human_notification_posted",
        "slept_seconds": 60,
    }


@pytest.mark.unit
async def test_monitor_operation_payload_redacts_secret_like_values(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    workspace_id = await seed_monitoring_workspace(factory, pr_number=80)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._execute(
        action=NotifyHuman(
            message=(
                "blocked with Bearer ghp_should_not_persist "
                "token=github_pat_should_not_persist password=sk-should-not-persist"
            )
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=80,
        status=_green_status(pr_number=80),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert len(operations) == 1
    persisted = f"{operations[0].payload!r} {operations[0].result!r}"
    assert "ghp_should_not_persist" not in persisted
    assert "github_pat_should_not_persist" not in persisted
    assert "sk-should-not-persist" not in persisted
    assert "Bearer" not in persisted
    assert "token=" not in persisted
    assert "password=" not in persisted
    assert "[redacted]" in persisted
