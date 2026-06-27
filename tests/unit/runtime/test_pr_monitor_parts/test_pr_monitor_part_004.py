"""Table-driven tests for ``pr_monitor.decide`` — CI-failure rerun budget.

Split out of ``test_pr_monitor_part_003.py`` to keep each test module under
the first-party line-count guardrail.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor import (
    AddressComments,
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReportCiFailure,
    RerunTransientCI,
    ReviewComment,
    ReviewThread,
    _ci_transient_rerun_state_key,
    _should_rerun_transient_ci,
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
    closed: bool = False,
    merged: bool = False,
) -> PRStatus:
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
        closed=closed,
        merged=merged,
    )


class TestCiFailure:
    @pytest.mark.unit
    def test_rerun_state_key_uses_structured_failure_signature(self) -> None:
        left = (
            CheckFailure(
                name="lint:type",
                conclusion="FAILURE",
                log_excerpt="HTTP 502",
                run_id="run",
            ),
        )
        right = (
            CheckFailure(
                name="type",
                conclusion="FAILURE",
                log_excerpt="HTTP 502",
                run_id="run:lint",
            ),
        )

        assert _ci_transient_rerun_state_key("head", left) != _ci_transient_rerun_state_key(
            "head",
            right,
        )

    @pytest.mark.unit
    def test_transient_failure_parks_for_human_after_rerun_budget(self) -> None:
        failure = CheckFailure(
            name="CI",
            conclusion="FAILURE",
            log_excerpt="curl: (56) Recv failure: Connection reset by peer",
            run_id="25655330295",
        )
        status = _status(check_state=CheckState.FAILURE, ci_failures=(failure,))
        state = MonitorState()
        state.threads_addressed_ids[
            _ci_transient_rerun_state_key(status.head_sha, status.ci_failures)
        ] = "2"

        action = decide(status, state, MonitorConfig(ci_transient_rerun_max_attempts=2))

        assert isinstance(action, NotifyHuman)
        assert action.message is not None
        assert "transient or infrastructure-related" in action.message

    @pytest.mark.unit
    def test_transient_rerun_budget_reads_legacy_rollup_signature(self) -> None:
        failure = CheckFailure(
            name="python-full-coverage",
            conclusion="FAILURE",
            log_excerpt="HTTP status server error (502 Bad Gateway)",
            run_id="25655330295",
        )
        rollup_failure = CheckFailure(
            name="ci-required",
            conclusion="FAILURE",
            log_excerpt="A required CI job did not pass.",
            run_id="25655330295",
        )
        status = _status(
            check_state=CheckState.FAILURE,
            ci_failures=(failure, rollup_failure),
        )
        state = MonitorState()
        state.threads_addressed_ids[
            _ci_transient_rerun_state_key(status.head_sha, status.ci_failures)
        ] = "2"

        action = decide(status, state, MonitorConfig(ci_transient_rerun_max_attempts=2))

        assert isinstance(action, NotifyHuman)
        assert action.message is not None
        assert "transient or infrastructure-related" in action.message

    @pytest.mark.unit
    def test_transient_failure_with_disabled_rerun_budget_notifies_human(self) -> None:
        failure = CheckFailure(
            name="CI",
            conclusion="FAILURE",
            log_excerpt="HTTP status server error (503 Service Unavailable)",
            run_id="25655330295",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(ci_transient_rerun_max_attempts=0),
        )

        assert isinstance(action, NotifyHuman)
        assert action.message is not None
        assert "transient or infrastructure-related" in action.message

    @pytest.mark.unit
    def test_transient_failure_corrupt_rerun_count_is_treated_as_zero(self) -> None:
        failure = CheckFailure(
            name="CI",
            conclusion="FAILURE",
            log_excerpt="curl: (56) Recv failure: Connection reset by peer",
            run_id="25655330295",
        )
        status = _status(check_state=CheckState.FAILURE, ci_failures=(failure,))
        state = MonitorState()
        state.threads_addressed_ids[
            _ci_transient_rerun_state_key(status.head_sha, status.ci_failures)
        ] = "not-an-int"

        action = decide(status, state, MonitorConfig())

        assert isinstance(action, RerunTransientCI)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_infra_assert_log_does_not_mask_transient_ci_rerun(self) -> None:
        failure = CheckFailure(
            name="CI",
            conclusion="FAILURE",
            log_excerpt="assert passed while reconnecting runner\nHTTP 502 Bad Gateway",
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
    def test_structured_test_evidence_prevents_transient_ci_rerun(self) -> None:
        failure = CheckFailure(
            name="python-full-coverage",
            conclusion="FAILURE",
            log_excerpt="curl: (56) Recv failure: Connection reset by peer",
            run_id="25655330295",
            test_node_ids=("pkg/tests/test_api.py::test_x",),
            assertion_snippets=("E   AssertionError: boom",),
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_transient_rerun_helper_rejects_empty_failure_snapshot(self) -> None:
        assert not _should_rerun_transient_ci(
            _status(check_state=CheckState.FAILURE, ci_failures=()),
            MonitorState(),
            MonitorConfig(),
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("run_id", [None, ""])
    def test_transient_failure_without_run_id_notifies_human(
        self,
        run_id: str | None,
    ) -> None:
        failure = CheckFailure(
            name="CI",
            conclusion="FAILURE",
            log_excerpt="HTTP status server error (502 Bad Gateway)",
            run_id=run_id,
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, NotifyHuman)
        assert action.message is not None
        assert "transient or infrastructure-related" in action.message

    @pytest.mark.unit
    def test_code_like_failure_still_dispatches_agent_repair(self) -> None:
        failure = CheckFailure(
            name="python-full-coverage",
            conclusion="FAILURE",
            log_excerpt="FAILED tests/unit/test_thing.py::test_case - AssertionError",
            run_id="25655330295",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_failure_with_per_check_details(self) -> None:
        failure = CheckFailure(
            name="playwright", conclusion="FAILURE", log_excerpt="Error: timeout"
        )
        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_failure_with_empty_failure_list_notifies_human(self) -> None:
        """A red rollup without fetched logs must not launch an agent repair."""
        action = decide(
            _status(check_state=CheckState.FAILURE),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, NotifyHuman)
        assert action.message is not None
        assert "could not retrieve actionable" in action.message

    @pytest.mark.unit
    def test_unresolved_comments_take_priority_over_ci_failure(self) -> None:
        """A new comment arriving mid-CI-fail means: address the comment
        first; the next push triggers CI anew. Otherwise we'd keep
        retrying CI fixes for stale commits."""
        t = _thread()
        action = decide(
            _status(check_state=CheckState.FAILURE, inline=(t,)),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, AddressComments)
