"""Unit tests for helpers in ``pr_monitor_runner``.

These cover the pure, side-effect-free helpers: ``_parse_verdict`` (CLI
reply → structured verdict) and ``_collect_defer_items`` (PRStatus +
MonitorState → bot/human defer buckets for the terminal artifact). The
full runner loop is exercised in
``tests/integration/runtime/test_pr_monitor_runner.py`` — this file
keeps the tight, no-I/O cases alongside the rest of the unit suite so
they run in the fast default lane.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckTiming,
    MergeStateStatus,
    MonitorState,
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
    _notify_human_reason,
    _parse_verdict,
    _stale_pending_check_warning_key,
    _stale_pending_check_warnings,
    _target_reconcile_payload,
    _with_ci_failures,
)
from tests.unit.runtime.test_pr_monitor import _status


def _monitor_runner(tmp_path: Path, fake: FakeCommandRunner) -> PullRequestMonitorRunner:
    return PullRequestMonitorRunner(
        session_factory=object(),  # type: ignore[arg-type]
        runner=fake,
        adapter=object(),  # type: ignore[arg-type]
        gh=object(),  # type: ignore[arg-type]
        worktrees_root=tmp_path / "work" / "git" / "worktrees",
    )


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
