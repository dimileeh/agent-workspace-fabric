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
    ReportCiFailure,
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

        assert isinstance(action, RerunTransientCI), action
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

        assert isinstance(action, RerunTransientCI), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_permanent_pull_detail_after_summary_dispatches_report_ci_failure(
        self,
    ) -> None:
        """A permanent 'failed to pull image' detail that follows the 'Docker pull
        failed' summary must prevent the summary from anchoring a transient rerun.
        Docker sometimes emits the summary before the containerd/kubelet detail, so
        a manifest-unknown / not-found detail within the forward window of a summary
        means the same pull is permanent (PRRT_kwDOSJAM6s6HsKWl)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull myapp:v99\n"
                "Docker pull failed with exit code 1\n"
                'Failed to pull image "myapp:v99": manifest unknown\n'
                "context deadline exceeded"
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
    def test_permanent_pull_detail_after_summary_not_found_dispatches_report_ci_failure(
        self,
    ) -> None:
        """Variant of the forward-probe regression: 'not found' on the detail line
        after a 'Docker pull failed' summary must also be classified permanent
        (PRRT_kwDOSJAM6s6HsKWl)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull myapp:v99\n"
                "Docker pull failed with exit code 1\n"
                'Failed to pull image "myapp:v99": not found\n'
                "context deadline exceeded"
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
    def test_permanent_pull_detail_after_summary_no_echo_dispatches_report_ci_failure(
        self,
    ) -> None:
        """A permanent 'failed to pull image' detail that follows a 'Docker pull
        failed' summary must mark the summary permanent even when the log stream
        did not echo the 'docker pull <ref>' command (no-echo wrapper).  Without
        the echo, _forward_detail_ref_matches_pull used to return False and the
        summary was left as transient, burning retry budget on a missing tag
        (PRRT_kwDOSJAM6s6Hti1K)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "context deadline exceeded\n"
                "Docker pull failed with exit code 1\n"
                'Failed to pull image "myapp:v99": manifest unknown\n'
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
    def test_permanent_pull_detail_after_summary_new_command_echo_dispatches_rerun(
        self,
    ) -> None:
        """A permanent 'failed to pull image' detail separated from the 'Docker pull
        failed' summary by a new 'docker pull' command echo belongs to a different
        pull operation and must not block RerunTransientCI for the summary
        (PRRT_kwDOSJAM6s6HsKWl)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull myapp:v99\n"
                "context deadline exceeded\n"
                "Docker pull failed with exit code 1\n"
                "/usr/bin/docker pull broken:missing\n"
                'Failed to pull image "broken:missing": manifest unknown'
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
    def test_permanent_daemon_error_before_daemon_timeout_different_image_dispatches_rerun(
        self,
    ) -> None:
        """A permanent daemon error for one image must not make a daemon registry
        timeout for a *different* image permanent.  The backward probe must be
        limited to 'docker pull failed' summary lines so that a preceding
        manifest-unknown / not-found daemon error does not misattribute a genuine
        registry timeout (PRRT_kwDOSJAM6s6HsNGM)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: manifest for broken-app:missing not found: manifest unknown\n"
                'Error response from daemon: Get "https://registry-1.docker.io/v2/library/postgres/manifests/16":'
                " context deadline exceeded"
            ),
            run_id="27091023772",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_stale_echo_for_earlier_image_does_not_make_later_summary_permanent(
        self,
    ) -> None:
        """A ``failed to pull image "<imageA>"`` permanent detail following a
        ``docker pull failed`` summary must not make the summary permanent when the
        only matching ``docker pull <imageA>`` echo predates an intervening
        ``docker pull <imageB>`` echo.  The summary belongs to the imageB pull; the
        backward ref-match search in ``_forward_detail_ref_matches_pull`` must start
        from the most-recent echo (imageB), not from the beginning of the log.
        With ``back_start=0`` the stale imageA echo at line 0 paired with the
        imageA detail, misclassifying a transient registry timeout as permanent and
        blocking a legitimate rerun (PRRT_kwDOSJAM6s6Hse5B)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull ghcr.io/org/app:v1\n"  # index 0 — earlier echo for imageA
                "/usr/bin/docker pull postgres:16\n"  # index 1 — echo for imageB (current)
                "context deadline exceeded\n"  # index 2 — imageB registry timeout
                "Docker pull failed with exit code 1\n"  # index 3 — summary for imageB
                'Failed to pull image "ghcr.io/org/app:v1": access denied'  # index 4 — stale detail
            ),
            run_id="27091023772",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_forward_permanent_detail_different_image_dispatches_rerun(
        self,
    ) -> None:
        """An unrelated kubelet/containerd 'failed to pull image' permanent error
        must not make an earlier transient Docker pull failure permanent when the
        image refs differ.  The forward probe must confirm the detail line targets
        the same image ref as the preceding 'docker pull <ref>' command echo
        (PRRT_kwDOSJAM6s6HsT-2)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "context deadline exceeded\n"
                "Docker pull failed with exit code 1\n"
                'Failed to pull image "app:v99": manifest unknown'
            ),
            run_id="27091023772",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_docker_pull_access_denied_near_unrelated_timeout_reports_ci_failure(
        self,
    ) -> None:
        """Canonical bug scenario from issue #452: a permanent access-denied pull error
        followed by an unrelated ``context deadline exceeded`` within the evidence
        window must NOT be treated as transient infra and rerun.  The image will never
        appear — burning the rerun budget is worse than reporting it to the agent."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: pull access denied for myimage\n"
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

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_docker_pull_access_denied_with_trailing_comma_reports_ci_failure(
        self,
    ) -> None:
        """Docker Hub appends a comma directly to the image name in access-denied messages:
        ``pull access denied for postgres:16, repository does not exist...``
        The split token ``"postgres:16,"`` must still match ``preceding_pull_image``
        so the failure is not silently skipped and misclassified as transient
        (PRRT_kwDOSJAM6s6HtPhs)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "Docker pull failed with exit code 1\n"
                "Error response from daemon: pull access denied for postgres:16,"
                " repository does not exist or may require 'docker login': denied: denied\n"
                "context deadline exceeded"
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
    def test_docker_pull_no_such_image_near_unrelated_timeout_reports_ci_failure(
        self,
    ) -> None:
        """A permanent ``No such image`` pull error with a nearby unrelated timeout
        must be classified deterministic (not transient) even though a
        ``context deadline exceeded`` sits within the evidence window.  The image
        does not exist; retrying is futile."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: No such image: nonexistent-app:latest\n"
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

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_docker_pull_manifest_unknown_near_unrelated_timeout_reports_ci_failure(
        self,
    ) -> None:
        """A permanent ``manifest unknown`` pull error with a nearby unrelated
        timeout must be classified deterministic.  The tag/digest does not exist
        in the registry; a rerun will not fix it."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: manifest for myapp:v99 not found: "
                "manifest unknown: manifest unknown\n"
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

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_summary_first_then_daemon_access_denied_reports_ci_failure(self) -> None:
        """When a log stream emits the ``docker pull failed`` summary *before* the
        daemon permanent error line (reversed from the typical CLI ordering), the
        forward daemon probe must detect the access-denied response and classify
        the pull as permanent — not retryable — so an adjacent unrelated
        ``context deadline exceeded`` does not trigger a wasteful rerun."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Docker pull failed with exit code 1\n"
                "Error response from daemon: pull access denied for myimage\n"
                "context deadline exceeded"
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
    def test_registry_http_trailing_denied_near_unrelated_timeout_reports_ci_failure(
        self,
    ) -> None:
        """A daemon denial that uses the HTTP-response form ``": denied"`` (colon-space
        before "denied", nothing after) must be classified permanent.  The ``denied:``
        marker alone misses this format because the daemon embeds the registry URL in
        quotes — e.g. ``Error response from daemon: Head "https://ghcr.io/v2/...":
        denied`` — and after quoted-string stripping only ``: denied`` remains.  A
        nearby unrelated ``context deadline exceeded`` must NOT trigger RerunTransientCI
        for a non-retryable auth failure (PRRT_kwDOSJAM6s6Hsmgv)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                'Error response from daemon: Head "https://ghcr.io/v2/org/app/manifests/latest": denied\n'
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

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_forward_daemon_denied_via_v2_url_with_preceding_pull_echo_reports_ci_failure(
        self,
    ) -> None:
        """Forward daemon probe must detect a permanent `: denied` response even
        when the daemon error embeds the image ref as a registry API URL rather
        than quoting it directly.

        Daemon auth/manifest errors commonly take the form::

            Error response from daemon: Head "https://ghcr.io/v2/org/app/manifests/latest": denied

        When a ``docker pull ghcr.io/org/app:latest`` echo precedes the summary,
        ``preceding_pull_image`` is set to ``ghcr.io/org/app:latest``.  That ref
        is not a whitespace-delimited token in the daemon line (``split()`` yields
        the quoted URL as one token), so the token check alone fails and the
        summary is incorrectly treated as transient.  The fix also checks the
        ``/v2/<repo>/`` URL fragment derived from the image ref
        (PRRT_kwDOSJAM6s6HsvOj)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull ghcr.io/org/app:latest\n"
                "Docker pull failed with exit code 1\n"
                'Error response from daemon: Head "https://ghcr.io/v2/org/app/manifests/latest": denied\n'
                "context deadline exceeded"
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
    def test_forward_permanent_detail_beyond_timeout_window_reports_ci_failure(
        self,
    ) -> None:
        """A ``failed to pull image "<ref>": manifest unknown`` detail that follows
        a ``docker pull failed`` summary must be detected as permanent even when the
        corresponding ``docker pull <ref>`` command echo is more than
        ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines before the summary.

        Regression for the bug where the forward detail probe passed
        ``start = index - window`` as the backward search bound for
        ``_forward_detail_ref_matches_pull``, causing it to miss the command echo and
        return False — so the manifest-unknown permanent failure was misclassified as
        transient and triggered an unnecessary rerun."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull app:v99\n"  # index 0 — echo, > 2 lines before summary
                "Pulling from library/app\n"  # index 1 — pull progress
                "context deadline exceeded\n"  # index 2 — unrelated timeout in log
                "Docker pull failed with exit code 1\n"  # index 3 — summary
                'Failed to pull image "app:v99": manifest unknown'  # index 4 — permanent detail
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
    def test_image_name_containing_denied_with_timeout_dispatches_rerun(self) -> None:
        """A Docker image whose name contains "denied:" as a substring must not
        cause a transient pull timeout to be classified as a permanent failure.
        Quoted substrings (the image reference) are stripped before checking
        permanent-error markers so only real error phrases outside the image name
        are matched."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull my-denied:latest\n"
                "Docker pull failed with exit code 1\n"
                'Failed to pull image "my-denied:latest": context deadline exceeded'
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
    def test_image_name_containing_unauthorized_with_timeout_dispatches_rerun(
        self,
    ) -> None:
        """An image whose name contains "unauthorized" must not prevent a genuine
        transient network timeout from being rerun.  The marker check must ignore
        the quoted image reference in the log line."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull unauthorized-service:latest\n"
                "Docker pull failed with exit code 1\n"
                'Failed to pull image "unauthorized-service:latest": context deadline exceeded'
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
    def test_interleaved_evidence_does_not_save_wrapped_kubelet_timeout(
        self,
    ) -> None:
        """A kubelet-style event whose header and wrapped ``context deadline
        exceeded`` line are split by an unrelated transient ``docker pull failed``
        summary must still reach the repair agent.  The uncorroborated kubelet
        header (index 0) precedes the timeout (index 2), so the timeout belongs
        to that kubelet event regardless of the interleaved evidence line at
        index 1.  Regression for the 'evidence between' heuristic that previously
        skipped exclusion when any evidence line fell strictly between the timeout
        and the preceding uncorroborated pull, silently rerunnig a real deploy bug."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                'Failed to pull image "app"\n'  # uncorroborated index 0
                "Docker pull failed with exit code 1\n"  # evidence index 1 (interleaved)
                "context deadline exceeded"  # index 2 — wrapped kubelet error
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
    def test_later_pull_echo_does_not_retroactively_corroborate_earlier_failure(
        self,
    ) -> None:
        """A ``docker pull <ref>`` echo that appears *after* a kubelet ``failed to
        pull image "<ref>"`` event must not corroborate that failure.  Only pull
        echoes that precede the failure line are valid evidence; a later echo would
        turn a real deploy bug into RerunTransientCI (PRRT_kwDOSJAM6s6HsnA_)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                'Failed to pull image "app:v99": manifest unknown\n'
                "/usr/bin/docker pull app:v99\n"
                "context deadline exceeded"
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
    def test_forward_daemon_denied_with_quiet_flag_in_pull_echo_reports_ci_failure(
        self,
    ) -> None:
        """When the pull echo uses ``--quiet`` before the image ref, the forward
        daemon probe must still extract the correct image and classify the pull as
        permanent.  Without flag-skipping logic, ``preceding_pull_image`` would be
        set to ``"--quiet"`` instead of ``"postgres:16"``, causing the daemon error
        to go unmatched and the failure to be misclassified as transient."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull --quiet postgres:16\n"
                "Docker pull failed with exit code 1\n"
                "Error response from daemon: pull access denied for postgres:16\n"
                "context deadline exceeded"
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
    def test_forward_daemon_denied_with_trailing_comma_in_daemon_error_reports_ci_failure(
        self,
    ) -> None:
        """Docker Hub appends a comma to the image name in access-denied messages when
        emitted after the pull summary (forward probe ordering).  The token
        ``"postgres:16,"`` must match ``preceding_pull_image = "postgres:16"`` so
        the permanent failure is not misclassified as transient (PRRT_kwDOSJAM6s6HtPhs)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "Docker pull failed with exit code 1\n"
                "Error response from daemon: pull access denied for postgres:16,"
                " repository does not exist or may require 'docker login': denied: denied\n"
                "context deadline exceeded"
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
    def test_forward_daemon_denied_with_platform_flag_in_pull_echo_reports_ci_failure(
        self,
    ) -> None:
        """When the pull echo uses ``--platform linux/amd64`` before the image ref,
        the forward daemon probe must skip both the flag and its value argument and
        extract the correct image ``postgres:16``.  Without this, ``preceding_pull_image``
        would be ``"linux/amd64"`` (not the image name), failing the daemon-error
        match and causing the permanent failure to be misclassified as transient."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull --platform linux/amd64 postgres:16\n"
                "Docker pull failed with exit code 1\n"
                "Error response from daemon: pull access denied for postgres:16\n"
                "context deadline exceeded"
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
    def test_forward_daemon_denied_via_hub_library_v2_url_reports_ci_failure(
        self,
    ) -> None:
        """Forward daemon probe must detect a permanent denial when the daemon error
        embeds a Docker Hub library image as a ``/v2/library/<name>/`` URL.

        Docker Hub official images (e.g. ``postgres:16``) have no registry host or
        namespace in the image ref, but the daemon emits distribution-API URLs of the
        form ``/v2/library/postgres/manifests/16``.  When the daemon error is
        URL-style (not a whitespace token), ``_image_ref_matches_daemon_url`` must
        also probe the ``/v2/library/<name>/`` fragment; otherwise the pull is
        misclassified as transient and triggers an unnecessary RerunTransientCI.

        Regression for PRRT_kwDOSJAM6s6Hs3NB."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "Docker pull failed with exit code 1\n"
                'Error response from daemon: Head "https://registry-1.docker.io/v2/library/postgres/manifests/16": denied\n'
                "context deadline exceeded"
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
    def test_stale_echo_before_window_does_not_suppress_permanent_daemon_error(
        self,
    ) -> None:
        """A stale ``docker pull <imageA>`` echo that precedes the evidence window
        must not suppress a permanent daemon error for a *different* image within
        the window.

        When the only ``docker pull`` echo is outside the
        ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` (e.g. a successful setup pull from
        an earlier step), ``preceding_pull_image`` must be left as ``None`` so the
        backward probe is not gated on the stale image identity.  Previously the
        stale ``postgres:16`` echo was extracted and the ``pull access denied for
        private-app`` daemon error was rejected because it did not mention
        ``postgres:16``, causing the summary to be misclassified as transient and
        a ``RerunTransientCI`` to be dispatched instead of ``ReportCiFailure``
        (PRRT_kwDOSJAM6s6HtCmc)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"  # line 0 — stale echo, outside window
                "Error response from daemon: pull access denied for private-app\n"  # line 1
                "context deadline exceeded\n"  # line 2 — unrelated timeout
                "Docker pull failed with exit code 1"  # line 3 — summary (index=3, start=1)
            ),
            run_id="27091023772",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_backward_daemon_error_different_image_does_not_make_summary_permanent(
        self,
    ) -> None:
        """An unrelated daemon permanent error appearing immediately before a
        'docker pull failed' summary must not mark it permanent when the preceding
        pull echo identifies a different image.

        Mixed-image step logs can contain a daemon error for image-B (e.g. a
        concurrent kubelet event) within the evidence window of a transient
        'docker pull failed' summary for image-A.  The no-intervening-echo guard
        alone cannot distinguish this from a genuine same-pull error when no new
        pull echo sits between the daemon error and the summary.  The backward probe
        must also check image identity against the preceding pull echo so that
        RerunTransientCI is still dispatched for the real registry timeout
        (PRRT_kwDOSJAM6s6Hs7GB)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "Error response from daemon: pull access denied for unrelated-app\n"
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

        assert isinstance(action, RerunTransientCI), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_stale_echo_before_window_does_not_reject_forward_permanent_detail(
        self,
    ) -> None:
        """A stale ``docker pull <imageA>`` echo outside the evidence window must not
        cause the forward detail probe to reject a no-echo permanent detail for a
        different image.

        When the only pull echo precedes the evidence window, ``preceding_pull_image``
        is correctly left as ``None``, but the stale ``back_start`` was still passed
        to ``_forward_detail_ref_matches_pull``, which scanned the full log from
        ``back_start`` and found the stale echo.  Since the detail's image ref
        (``myapp:v99``) did not match the stale echo ref (``postgres:16``), the
        helper returned False and the permanent forward detail was ignored, causing
        the summary to be misclassified as transient and rerun instead of reported
        (PRRT_kwDOSJAM6s6HtwnG)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"  # line 0 — stale echo, outside window
                "Status: Downloaded newer image for postgres:16\n"  # line 1 — unrelated
                "Setting up test environment\n"  # line 2 — unrelated (pushes summary to index 4)
                "context deadline exceeded\n"  # line 3 — timeout
                "Docker pull failed with exit code 1\n"  # line 4 — summary (start=2, stale echo at 0)
                'Failed to pull image "myapp:v99": manifest unknown\n'  # line 5 — permanent forward detail
            ),
            run_id="27091023772",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_stale_echo_for_different_image_does_not_block_permanent_detail(
        self,
    ) -> None:
        """A stale pull echo for a *different* image must not block the permanent
        detail for the actual failing image when the stale pull's own summary
        already appeared between the stale echo and the current summary.

        When the stale echo is for image A (which already emitted its own
        "docker pull failed" summary earlier in the log), a later "docker pull
        failed" summary for image B (no echo in window) must not have its
        permanent forward detail rejected because image B does not match the
        stale image A.  Previously stale_guard_image was set to image A
        regardless of whether image A had a prior summary, so the no-echo
        fallback in ``_forward_detail_ref_matches_pull`` rejected image B's
        manifest-unknown detail and misclassified the failure as transient,
        triggering RerunTransientCI instead of ReportCiFailure
        (PRRT_kwDOSJAM6s6HuLfZ)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "docker pull stale-image:v1\n"  # line 0 — stale echo for a DIFFERENT image
                # line 1: stale image already summarised with a permanent marker; its
                # "docker pull failed" presence proves the stale pull is accounted for,
                # so the current summary (line 4) belongs to a different pull.
                "docker pull failed with exit code 1, pull access denied\n"
                "context deadline exceeded\n"  # line 2 — timeout (in window [2,4] for line 4)
                "context deadline exceeded\n"  # line 3 — timeout
                "docker pull failed with exit code 1\n"  # line 4 — summary (index=4, start=2)
                'failed to pull image "actual-image:v2": manifest unknown\n'  # line 5 — permanent
            ),
            run_id="27091023772",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_stale_echo_without_success_rejects_unrelated_forward_permanent_detail(
        self,
    ) -> None:
        """A stale pull echo whose pull did NOT succeed must constrain the forward
        detail probe so an unrelated kubelet 'failed to pull image' event for a
        different image does not misclassify the transient timeout as permanent.

        When the nearest echo is outside the evidence window and there is no
        success marker for its image between the echo and the summary, the
        current 'docker pull failed' likely belongs to the stale pull.
        Previously the no-echo fallback in ``_forward_detail_ref_matches_pull``
        accepted any permanent detail in the forward window, so a kubelet event
        for an unrelated image caused a ReportCiFailure instead of a
        RerunTransientCI (PRRT_kwDOSJAM6s6Ht11n)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"  # line 0 — stale echo, outside window, no success
                "context deadline exceeded\n"  # line 1
                "context deadline exceeded\n"  # line 2
                "Docker pull failed with exit code 1\n"  # line 3 — summary (index=3, start=1, stale echo at 0)
                'Failed to pull image "unrelated-app:v1": manifest unknown\n'  # line 4 — unrelated kubelet event
            ),
            run_id="27091023772",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_tagless_stale_echo_rejects_different_tag_forward_detail(
        self,
    ) -> None:
        """A tagless stale pull (``docker pull ghcr.io/org/app``) defaults to
        ``:latest`` per Docker docs.  A forward detail for a *different* tag
        (``ghcr.io/org/app:bad``) must not match the stale guard — accepting any
        tag of the same repo would mark a transient registry timeout as permanent
        when an unrelated kubelet event for the same repo but a different tag
        appears in the forward window (PRRT_kwDOSJAM6s6Hujrw)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "docker pull ghcr.io/org/app\n"  # line 0 — tagless stale echo, no success
                "context deadline exceeded\n"  # line 1
                "context deadline exceeded\n"  # line 2
                "Docker pull failed with exit code 1\n"  # line 3 — summary (stale echo at 0)
                'Failed to pull image "ghcr.io/org/app:bad": manifest unknown\n'  # line 4 — wrong tag
            ),
            run_id="27091023773",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_tagless_stale_echo_accepts_latest_tag_forward_detail(
        self,
    ) -> None:
        """A tagless stale pull (``docker pull ghcr.io/org/app``) must still match
        a forward detail for ``:latest`` because Docker treats a tagless pull as
        ``:latest`` (PRRT_kwDOSJAM6s6Hujrw)."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "docker pull ghcr.io/org/app\n"  # line 0 — tagless stale echo, no success
                "context deadline exceeded\n"  # line 1
                "context deadline exceeded\n"  # line 2
                "Docker pull failed with exit code 1\n"  # line 3 — summary (stale echo at 0)
                'Failed to pull image "ghcr.io/org/app:latest": manifest unknown\n'  # line 4 — :latest permanent
            ),
            run_id="27091023774",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_permanent_pull_detail_before_summary_reports_ci_failure(
        self,
    ) -> None:
        """A permanent ``failed to pull image "<ref>": manifest unknown`` detail
        that appears *before* the ``docker pull failed`` summary must prevent the
        summary from anchoring a transient rerun, even when an unrelated
        ``context deadline exceeded`` is nearby.

        Before the fix, the backward probe only considered daemon-error lines.
        The detail was classified as permanent evidence on its own line but the
        *summary* line was not marked permanent, leaving a nearby timeout free to
        anchor a spurious RerunTransientCI (PRRT_kwDOSJAM6s6HvFgf)."""
        failure = CheckFailure(
            name="python-coverage-shards (1)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull myapp:missing\n"  # line 0 — pull echo
                'Failed to pull image "myapp:missing": manifest unknown\n'  # line 1 — backward detail
                "Docker pull failed with exit code 1\n"  # line 2 — summary
                "context deadline exceeded"  # line 3 — unrelated timeout
            ),
            run_id="27091023775",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_permanent_detail_for_earlier_pull_before_summary_does_not_block_rerun(
        self,
    ) -> None:
        """A permanent ``failed to pull image`` detail for a *different, earlier*
        pull that is separated from the current summary by a new ``docker pull``
        command echo must not make the summary permanent.

        The backward detail probe's no-intervening-pull guard must reject the
        detail when a new pull echo appears between the detail and the summary —
        that echo marks the start of the current (transient) pull, so the earlier
        pull's permanent detail does not influence the current summary
        (PRRT_kwDOSJAM6s6HvFgf)."""
        failure = CheckFailure(
            name="python-coverage-shards (1)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull other:bad\n"  # line 0 — first pull echo
                'Failed to pull image "other:bad": manifest unknown\n'  # line 1 — permanent for first pull
                "/usr/bin/docker pull myapp:latest\n"  # line 2 — second pull echo (intervening)
                "context deadline exceeded\n"  # line 3 — transient timeout for myapp
                "Docker pull failed with exit code 1\n"  # line 4 — summary for myapp
            ),
            run_id="27091023776",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, RerunTransientCI), action
        assert action.failures == (failure,)
