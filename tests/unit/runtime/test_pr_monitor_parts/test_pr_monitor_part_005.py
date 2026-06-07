"""Table-driven tests for ``pr_monitor.decide`` — CI-failure handling (continued).

Split out of ``test_pr_monitor_part_003.py`` to keep each test module under
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
    RerunTransientCI,
    ReviewComment,
    ReviewThread,
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
    """Tests for CI-failure decide() paths (overflow from part_003)."""

    @pytest.mark.unit
    def test_unrelated_permanent_daemon_error_after_transient_pull_dispatches_rerun(
        self,
    ) -> None:
        """An unrelated permanent daemon error appearing immediately *after* a
        transient pull-failure summary must not cause the transient pull to be
        classified as permanent.  The daemon error belongs to a different image pull
        that begins after the transient failure; the backward-only probe restriction
        prevents it from being attributed to the earlier evidence line
        (PRRT_kwDOSJAM6s6Hr82p)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "context deadline exceeded\n"
                "Docker pull failed with exit code 1\n"
                "Error response from daemon: manifest for broken-app:missing not found: manifest unknown"
            ),
            run_id="27091023772",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_unrelated_permanent_daemon_error_before_transient_pull_with_new_command_dispatches_rerun(
        self,
    ) -> None:
        """An unrelated permanent daemon error appearing *before* a transient pull
        but separated from it by a new ``docker pull`` command echo must not block
        RerunTransientCI.  The intervening command echo signals a new pull invocation,
        so the permanent daemon error belongs to the earlier image, not to the later
        transient pull (PRRT_kwDOSJAM6s6Hr82p)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: No such image: broken-app:missing\n"
                "/usr/bin/docker pull postgres:16\n"
                "Docker pull failed with exit code 1\n"
                "context deadline exceeded"
            ),
            run_id="27091023772",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI)
        assert action.failures == (failure,)
