"""Table-driven tests for ``pr_monitor.decide`` — CI-failure rerun budget.

Split out of ``test_pr_monitor_part_003.py`` to keep each test module under
the first-party line-count guardrail.
"""

from __future__ import annotations

import pytest

import awf.runtime.pr_monitor as pr_monitor_module
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
    WaitForTransientCI,
    _ci_failure_identity,
    _ci_transient_infra_wait_count,
    _ci_transient_infra_wait_count_key,
    _ci_transient_infra_wait_exhausted,
    _ci_transient_infra_wait_seconds,
    _ci_transient_infra_wait_started_at,
    _ci_transient_infra_wait_started_key,
    _ci_transient_rerun_state_key,
    _record_ci_transient_infra_wait,
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
    def test_rerun_state_key_is_stable_when_run_id_present_despite_name_drift(self) -> None:
        # One poll resolves the failing run through ``gh run list`` and records
        # the workflow run name; a later poll misses it there and falls back to
        # the rollup check name (and a defaulted conclusion). Same ``run_id`` ->
        # the retry-budget key must not drift, or the rerun/wait budget resets.
        run_list_poll = (
            CheckFailure(
                name="CI / build",
                conclusion="FAILURE",
                log_excerpt="HTTP 502",
                run_id="25655330295",
            ),
        )
        rollup_fallback_poll = (
            CheckFailure(
                name="ci-required",
                conclusion="TIMED_OUT",
                log_excerpt="HTTP 502",
                run_id="25655330295",
            ),
        )

        assert _ci_transient_rerun_state_key(
            "head", run_list_poll
        ) == _ci_transient_rerun_state_key("head", rollup_fallback_poll)

    @pytest.mark.unit
    def test_transient_failure_enters_infra_wait_after_rerun_budget(self) -> None:
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

        assert isinstance(action, WaitForTransientCI)
        assert action.failures == (failure,)
        assert action.wait_count == 1
        assert action.wait_seconds == 60

    @pytest.mark.unit
    def test_transient_infra_wait_reads_legacy_rollup_signature(self) -> None:
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

        assert isinstance(action, WaitForTransientCI)
        assert action.failures == (failure,)

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
    def test_transient_infra_wait_cap_notifies_human(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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
        _record_ci_transient_infra_wait(
            state,
            head_sha=status.head_sha,
            failures=(failure,),
            now=100.0,
        )
        monkeypatch.setattr(pr_monitor_module.time, "time", lambda: 2000.0)

        action = decide(
            status,
            state,
            MonitorConfig(
                ci_transient_rerun_max_attempts=2,
                ci_transient_infra_wait_max_seconds=1800,
            ),
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


class TestCiTransientInfraWaitHelpers:
    """Direct coverage for the transient infra-wait state helpers.

    ``decide`` only reaches some of these branches through accumulated monitor
    state across many polls (corrupt markers, repeat waits, disabled caps), so
    they are exercised here against the helper contract directly rather than
    through a multi-poll integration path.
    """

    def _failure(self, run_id: str | None = "25655330295") -> CheckFailure:
        return CheckFailure(
            name="CI",
            conclusion="FAILURE",
            log_excerpt="curl: (56) Recv failure: Connection reset by peer",
            run_id=run_id,
        )

    @pytest.mark.unit
    def test_failure_identity_without_run_id_falls_back_to_name_conclusion(self) -> None:
        # A failing run with no ``run_id`` has no stable run identity, so the
        # name/conclusion pair is the budget key's only identity.
        assert _ci_failure_identity(self._failure(run_id=None)) == ("", "CI", "FAILURE")

    @pytest.mark.unit
    def test_infra_wait_count_treats_corrupt_marker_as_zero(self) -> None:
        failure = self._failure()
        state = MonitorState()
        state.threads_addressed_ids[_ci_transient_infra_wait_count_key("abc123", (failure,))] = (
            "not-an-int"
        )

        assert _ci_transient_infra_wait_count(state, head_sha="abc123", failures=(failure,)) == 0

    @pytest.mark.unit
    def test_infra_wait_started_at_treats_corrupt_marker_as_unset(self) -> None:
        failure = self._failure()
        state = MonitorState()
        state.threads_addressed_ids[_ci_transient_infra_wait_started_key("abc123", (failure,))] = (
            "not-a-float"
        )

        assert (
            _ci_transient_infra_wait_started_at(state, head_sha="abc123", failures=(failure,))
            is None
        )

    @pytest.mark.unit
    def test_record_infra_wait_preserves_first_seen_timestamp(self) -> None:
        # The first wait stamps ``started_at``; later waits only bump the count
        # and keep the original timestamp so the escalation deadline is stable.
        failure = self._failure()
        state = MonitorState()

        first_count, first_started = _record_ci_transient_infra_wait(
            state, head_sha="abc123", failures=(failure,), now=100.0
        )
        second_count, second_started = _record_ci_transient_infra_wait(
            state, head_sha="abc123", failures=(failure,), now=500.0
        )

        assert (first_count, first_started) == (1, 100.0)
        assert (second_count, second_started) == (2, 100.0)

    @pytest.mark.unit
    def test_infra_wait_exhausted_when_max_seconds_disabled(self) -> None:
        # ``ci_transient_infra_wait_max_seconds <= 0`` disables waiting entirely,
        # so the wait is always considered exhausted.
        failure = self._failure()
        status = _status(check_state=CheckState.FAILURE, ci_failures=(failure,))

        assert _ci_transient_infra_wait_exhausted(
            status,
            MonitorState(),
            MonitorConfig(ci_transient_infra_wait_max_seconds=0.0),
            (failure,),
        )

    @pytest.mark.unit
    def test_infra_wait_seconds_uncapped_when_backoff_cap_disabled(self) -> None:
        # A non-positive backoff cap disables capping, so the exponential wait is
        # returned in full instead of being clamped.
        failure = self._failure()
        status = _status(check_state=CheckState.FAILURE, ci_failures=(failure,))
        state = MonitorState()
        state.threads_addressed_ids[
            _ci_transient_infra_wait_count_key(status.head_sha, (failure,))
        ] = "5"
        config = MonitorConfig(
            poll_interval_seconds=60.0,
            ci_transient_infra_wait_max_backoff_seconds=0.0,
        )

        # 60 * 2**5 = 1920, well above the default 300s cap, returned uncapped.
        assert _ci_transient_infra_wait_seconds(status, state, config, (failure,)) == 1920.0
