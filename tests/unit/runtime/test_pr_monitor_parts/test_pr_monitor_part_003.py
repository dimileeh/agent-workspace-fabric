"""Table-driven tests for ``pr_monitor.decide`` — CI-failure handling.

Split out of ``test_pr_monitor_part_001.py`` to keep each test module under
the first-party line-count guardrail.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    PRStatus,
    ReportCiFailure,
    RerunTransientCI,
    ReviewComment,
    ReviewThread,
    WaitForCI,
    decide,
)


def _thread(
    tid: str = "T1",
    body: str = "fix this",
    is_resolved: bool = False,
    author: str | None = None,
) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path="src/x.py",
        line=10,
        body_excerpt=body,
        author=author,
        is_resolved=is_resolved,
    )


def _review(
    cid: str = "C1",
    body: str = "see below",
    is_resolved: bool = False,
    blocks_merge: bool = False,
    author: str | None = None,
    source_kind: str = "review",
    state: str | None = None,
) -> ReviewComment:
    return ReviewComment(
        comment_id=cid,
        body_excerpt=body,
        author=author,
        is_resolved=is_resolved,
        blocks_merge=blocks_merge,
        source_kind=source_kind,
        state=state,
    )


def _status(
    *,
    head_sha: str = "abc123",
    mergeable: MergeableState = MergeableState.MERGEABLE,
    check_state: CheckState = CheckState.SUCCESS,
    inline: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    blocking_reviews: tuple[ReviewComment, ...] | None = None,
    base_behind: int = 0,
    merge_state_status: MergeStateStatus = MergeStateStatus.CLEAN,
    ci_failures: tuple[CheckFailure, ...] = (),
    ci_runs_in_progress: bool = False,
    closed: bool = False,
    merged: bool = False,
) -> PRStatus:
    """Build a PRStatus fixture for CI failure monitor tests."""
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=mergeable,
        check_state=check_state,
        unresolved_inline_threads=inline,
        unresolved_review_comments=reviews,
        blocking_reviews=(
            tuple(c for c in reviews if c.blocks_merge)
            if blocking_reviews is None
            else blocking_reviews
        ),
        base_behind_count=base_behind,
        merge_state_status=merge_state_status,
        ci_failures=ci_failures,
        ci_runs_in_progress=ci_runs_in_progress,
        closed=closed,
        merged=merged,
    )


class TestCiFailure:
    """Tests for CiFailure."""

    @pytest.mark.unit
    def test_transient_failure_dispatches_rerun_before_agent_repair(self) -> None:
        """Verify transient failure dispatches rerun before agent repair."""
        failure = CheckFailure(
            name="CI",
            conclusion="FAILURE",
            log_excerpt=(
                "Set up Python\n"
                "error: Failed to download cpython-3.12.9\n"
                "HTTP status server error (502 Bad Gateway)"
            ),
            run_id="25655330295",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_transient_failure_with_required_rollup_reports_completed_failure_set(
        self,
    ) -> None:
        """Verify transient failure with required rollup reports completed failure set."""
        transient_failure = CheckFailure(
            name="python-full-coverage",
            conclusion="FAILURE",
            log_excerpt=(
                "error: Failed to install cpython-3.12.9-linux-x86_64-gnu\n"
                "Caused by: HTTP status server error (503 Service Unavailable)"
            ),
            run_id="25897584271",
        )
        rollup_failure = CheckFailure(
            name="ci-required",
            conclusion="FAILURE",
            log_excerpt=(
                "A required CI job did not pass.\n"
                "lint-and-type: success\n"
                "python-full-coverage: failure\n"
                "console: success\n"
                "release-artifacts: success"
            ),
            run_id="25897584271",
        )

        action = decide(
            _status(
                check_state=CheckState.FAILURE,
                ci_failures=(transient_failure, rollup_failure),
            ),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (transient_failure, rollup_failure)

    @pytest.mark.unit
    def test_transient_failure_with_synthesized_rollup_reruns_transient_job(
        self,
    ) -> None:
        """A no-log/no-run-id required-rollup sibling must not block transient rerun.

        Regression for PRRT_kwDOSJAM6s6Nad-D."""
        transient_failure = CheckFailure(
            name="python-full-coverage",
            conclusion="FAILURE",
            log_excerpt=(
                "error: Failed to install cpython-3.12.9-linux-x86_64-gnu\n"
                "Caused by: HTTP status server error (503 Service Unavailable)"
            ),
            run_id="25897584271",
        )
        rollup_failure = CheckFailure(
            name="ci-required",
            conclusion="FAILURE",
            log_excerpt="",
            run_id=None,
        )

        action = decide(
            _status(
                check_state=CheckState.FAILURE,
                ci_failures=(transient_failure, rollup_failure),
            ),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI)
        assert action.failures == (transient_failure,)

    @pytest.mark.unit
    def test_mixed_run_with_rollup_marker_and_transient_evidence_dispatches_rerun(
        self,
    ) -> None:
        """A single workflow run can fail a transient job *and* the ci-required
        rollup step. ``gh run view --log-failed`` then emits one combined log
        carrying a retryable 503/timeout marker alongside the rollup marker;
        the monitor must still rerun the run rather than parking the flake on
        ``NotifyHuman`` just because the excerpt also names the required-CI marker.

        Regression for PRRT_kwDOSJAM6s6MtULO."""
        mixed_failure = CheckFailure(
            name="CI",
            conclusion="FAILURE",
            log_excerpt=(
                "error: Failed to install cpython-3.12.9-linux-x86_64-gnu\n"
                "Caused by: HTTP status server error (503 Service Unavailable)\n"
                "A required CI job did not pass.\n"
                "python-full-coverage: failure\n"
                "console: success"
            ),
            run_id="25897584271",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(mixed_failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI)
        assert action.failures == (mixed_failure,)

    @pytest.mark.unit
    def test_required_rollup_without_underlying_transient_job_reports_failure(
        self,
    ) -> None:
        """Verify required rollup without underlying transient job reports failure."""
        rollup_failure = CheckFailure(
            name="ci-required",
            conclusion="FAILURE",
            log_excerpt=(
                "A required CI job did not pass.\n"
                "lint-and-type: success\n"
                "python-full-coverage: failure"
            ),
            run_id="25897584271",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(rollup_failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (rollup_failure,)

    @pytest.mark.unit
    def test_arbitrary_completed_ci_failure_reports_failure(self) -> None:
        """Verify arbitrary completed ci failure reports failure."""
        failure = CheckFailure(
            name="go-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "=== RUN   TestWidgetLifecycle\n"
                "widget_test.go:42: expected active widget, got archived\n"
                "--- FAIL: TestWidgetLifecycle (0.03s)"
            ),
            run_id="25897584272",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_completed_ci_failure_reports_while_other_runs_are_in_progress(self) -> None:
        """Verify completed ci failure reports while other runs are in progress."""
        failure = CheckFailure(
            name="go-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "=== RUN   TestWidgetLifecycle\n"
                "widget_test.go:42: expected active widget, got archived\n"
                "--- FAIL: TestWidgetLifecycle (0.03s)"
            ),
            run_id="25897584272",
        )

        action = decide(
            _status(
                check_state=CheckState.FAILURE,
                ci_failures=(failure,),
                ci_runs_in_progress=True,
            ),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_failed_ci_without_failure_evidence_waits_for_in_progress_runs(self) -> None:
        """Verify failed ci without failure evidence waits for in progress runs."""
        action = decide(
            _status(check_state=CheckState.FAILURE, ci_runs_in_progress=True),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, WaitForCI)
        assert action.reason == "ci_run_in_progress"

    @pytest.mark.unit
    def test_generic_transient_text_inside_pytest_failure_reports_failure(self) -> None:
        """Transient marker substrings inside real test failure output must not
        turn the entire check into a rerunnable CI flake."""
        failure = CheckFailure(
            name="integration-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "tests/integration/test_fetch.py::test_download_prompt FAILED\n"
                "E   AssertionError: failed to download prompt fixture\n"
                "E   assert 'try again later' == 'ready'\n"
                "=== short test summary info ===\n"
                "FAILED tests/integration/test_fetch.py::test_download_prompt"
            ),
            run_id="25897584272",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_docker_pull_registry_timeout_with_code_failure_reports_failure(self) -> None:
        """Combined logs with code failures and Docker timeouts must be repaired."""
        failure = CheckFailure(
            name="python-full-coverage",
            conclusion="FAILURE",
            log_excerpt=(
                "tests/integration/test_fetch.py::test_download_prompt FAILED\n"
                "E   AssertionError: expected 'ready'\n"
                "=== short test summary info ===\n"
                "FAILED tests/integration/test_fetch.py::test_download_prompt\n"
                "/usr/bin/docker pull postgres:16\n"
                'Error response from daemon: Get "https://registry-1.docker.io/v2/": '
                "context deadline exceeded (Client.Timeout exceeded while awaiting headers)\n"
                "Docker pull failed with exit code 1"
            ),
            run_id="27091023772",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_mixed_run_with_rollup_marker_and_code_evidence_reports_failure(
        self,
    ) -> None:
        """A single workflow run can fail both a real job and the ci-required
        rollup step. ``gh run view --log-failed`` then emits one combined log
        carrying code-failure evidence alongside the rollup marker; the monitor
        must still dispatch the repair agent rather than parking on
        ``NotifyHuman``."""
        mixed_failure = CheckFailure(
            name="CI",
            conclusion="FAILURE",
            log_excerpt=(
                "src/awf/foo.py:12: error: Incompatible return value type "
                "[return-value]\n"
                "Found type errors\n"
                "A required CI job did not pass.\n"
                "lint-and-type: failure\n"
                "console: success"
            ),
            run_id="25897584271",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(mixed_failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (mixed_failure,)

    @pytest.mark.unit
    def test_exhausted_transient_sibling_does_not_mask_actionable_code_evidence(
        self,
    ) -> None:
        """When the rerun budget is exhausted/disabled, a transient flake sibling
        must not short-circuit to ``NotifyHuman`` while a rollup-marked,
        code-bearing failure (filtered out of the rerun set) still carries
        fixable evidence. The repair agent must be dispatched."""
        transient_failure = CheckFailure(
            name="lint-and-type",
            conclusion="FAILURE",
            log_excerpt=(
                "error: Failed to install cpython-3.12.9-linux-x86_64-gnu\n"
                "Caused by: HTTP status server error (503 Service Unavailable)"
            ),
            run_id="25897584271",
        )
        mixed_failure = CheckFailure(
            name="python-full-coverage",
            conclusion="FAILURE",
            log_excerpt=(
                "src/awf/foo.py:12: error: Incompatible return value type "
                "[return-value]\n"
                "Found type errors\n"
                "A required CI job did not pass.\n"
                "python-full-coverage: failure\n"
                "console: success"
            ),
            run_id="25897584271",
        )

        action = decide(
            _status(
                check_state=CheckState.FAILURE,
                ci_failures=(transient_failure, mixed_failure),
            ),
            MonitorState(),
            MonitorConfig(ci_transient_rerun_max_attempts=0),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (transient_failure, mixed_failure)

    @pytest.mark.unit
    def test_default_budget_transient_sibling_does_not_mask_actionable_code_evidence(
        self,
    ) -> None:
        """With the default rerun budget still available, a transient flake sibling
        must not trigger ``RerunTransientCI`` while a separate rollup-marked,
        code-bearing failure (filtered out of the rerun set) still carries fixable
        pytest/mypy/ruff evidence. The repair agent must be dispatched instead of
        burning reruns on the flake until the budget clears.

        Regression for PRRT_kwDOSJAM6s6MtSBI."""
        transient_failure = CheckFailure(
            name="lint-and-type",
            conclusion="FAILURE",
            log_excerpt=(
                "error: Failed to install cpython-3.12.9-linux-x86_64-gnu\n"
                "Caused by: HTTP status server error (503 Service Unavailable)"
            ),
            run_id="25897584271",
        )
        mixed_failure = CheckFailure(
            name="python-full-coverage",
            conclusion="FAILURE",
            log_excerpt=(
                "src/awf/foo.py:12: error: Incompatible return value type "
                "[return-value]\n"
                "Found type errors\n"
                "A required CI job did not pass.\n"
                "python-full-coverage: failure\n"
                "console: success"
            ),
            run_id="25897584299",
        )

        action = decide(
            _status(
                check_state=CheckState.FAILURE,
                ci_failures=(transient_failure, mixed_failure),
            ),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (transient_failure, mixed_failure)

    @pytest.mark.unit
    def test_transient_rerun_skipped_when_no_run_id_sibling_has_code_evidence(
        self,
    ) -> None:
        """A rerunnable transient Actions failure must not win over a separate
        synthesized rollup row (no ``run_id``) that still carries fixable code
        evidence. The repair agent must be dispatched instead of burning reruns
        on the flake.

        Regression for PRRT_kwDOSJAM6s6NarzR."""
        transient_failure = CheckFailure(
            name="lint-and-type",
            conclusion="FAILURE",
            log_excerpt=(
                "error: Failed to install cpython-3.12.9-linux-x86_64-gnu\n"
                "Caused by: HTTP status server error (503 Service Unavailable)"
            ),
            run_id="25897584271",
        )
        rollup_failure = CheckFailure(
            name="python-full-coverage",
            conclusion="FAILURE",
            log_excerpt=(
                "src/awf/foo.py:12: error: Incompatible return value type "
                "[return-value]\n"
                "Found type errors\n"
                "A required CI job did not pass.\n"
                "python-full-coverage: failure\n"
                "console: success"
            ),
            run_id=None,
        )

        action = decide(
            _status(
                check_state=CheckState.FAILURE,
                ci_failures=(transient_failure, rollup_failure),
            ),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (transient_failure, rollup_failure)
