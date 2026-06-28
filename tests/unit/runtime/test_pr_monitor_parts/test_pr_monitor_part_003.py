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
    NotifyHuman,
    PRStatus,
    ReportCiFailure,
    RerunTransientCI,
    ReviewComment,
    ReviewThread,
    WaitForCI,
    _log_shows_docker_registry_timeout,
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
    @pytest.mark.unit
    def test_failed_check_with_ci_run_in_progress_waits_before_missing_log_escalation(
        self,
    ) -> None:
        action = decide(
            _status(
                check_state=CheckState.FAILURE,
                ci_failures=(),
                ci_runs_in_progress=True,
            ),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, WaitForCI)
        assert action.reason == "ci_run_in_progress"

    @pytest.mark.unit
    def test_no_failure_without_ci_run_in_progress_keeps_existing_terminal_path(self) -> None:
        action = decide(
            _status(check_state=CheckState.SUCCESS, ci_runs_in_progress=False),
            MonitorState(),
            MonitorConfig(auto_merge=False),
        )

        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_transient_failure_dispatches_rerun_before_agent_repair(self) -> None:
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
    def test_transient_failure_with_required_rollup_dispatches_rerun_for_underlying_job(
        self,
    ) -> None:
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
    def test_required_rollup_without_underlying_transient_job_notifies_human(
        self,
    ) -> None:
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

        assert isinstance(action, NotifyHuman)
        assert action.message is not None
        assert "without actionable code-failure evidence" in action.message

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
    def test_transient_tool_download_failure_dispatches_rerun(self) -> None:
        failure = CheckFailure(
            name="python-full-coverage",
            conclusion="FAILURE",
            log_excerpt=(
                "Install tools\n"
                "Failed to download ruff from PyPI\n"
                "curl: (56) Recv failure: Connection reset by peer"
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
    def test_docker_pull_registry_timeout_dispatches_rerun(self) -> None:
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                'Error response from daemon: Get "https://registry-1.docker.io/v2/": '
                "context deadline exceeded (Client.Timeout exceeded while awaiting headers)\n"
                "net/http: request canceled while waiting for connection "
                "(Client.Timeout exceeded while awaiting headers)\n"
                "Docker pull failed with exit code 1"
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
    def test_docker_pull_failed_wording_anchors_request_canceled_rerun(self) -> None:
        """A request-canceled timeout tied to an explicit ``docker pull failed``
        line (no daemon-error wrapper) is still a registry pull failure → rerun."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "net/http: request canceled while waiting for connection\n"
                "Docker pull failed with exit code 1"
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
    def test_docker_pull_failed_summary_does_not_corroborate_same_token_image_event(
        self,
    ) -> None:
        """A ``docker pull failed ...`` *failure summary* must not be mistaken for a
        ``docker pull <ref>`` *command echo*. Its ``split()`` tokens
        (``docker``/``pull``/``failed``/...) would otherwise let an adjacent kubelet
        ``Failed to pull image "docker"`` event — ``docker`` is a real Docker Hub
        image — match by token and be wrongly corroborated as Docker-CLI pull
        evidence, dropping it out of the uncorroborated set so its own
        ``context deadline exceeded`` timeout is no longer excluded. That real
        application-image deploy bug must reach the repair agent rather than be
        silently rerun as transient infra."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "=== RUN   TestDeployApp\n"
                '  Warning  Failed   kubelet  Failed to pull image "docker": '
                "context deadline exceeded\n"
                "Docker pull failed with exit code 1\n"
                "--- FAIL: TestDeployApp (120.00s)"
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
    def test_log_shows_docker_registry_timeout_lowercases_raw_text(self) -> None:
        """The helper is self-contained: it lowercases its own input, so raw
        mixed-case log text matches the all-lowercase marker tuples without the
        caller having to pre-lowercase. Guards against a future caller passing
        unnormalized text and silently getting a False negative."""
        raw_log = (
            "/usr/bin/docker pull postgres:16\n"
            'Error response from daemon: Get "https://registry-1.docker.io/v2/": '
            "context deadline exceeded (Client.Timeout exceeded while awaiting headers)\n"
            "Docker pull failed with exit code 1"
        )

        assert _log_shows_docker_registry_timeout(raw_log) is True
        # Pre-lowercased text (today's only caller) keeps returning True.
        assert _log_shows_docker_registry_timeout(raw_log.lower()) is True

    @pytest.mark.unit
    def test_docker_pull_echo_then_bare_request_canceled_reports_ci_failure(self) -> None:
        """A bare ``docker pull`` *command* echo followed by a request-canceled
        timeout — with no daemon-error or pull-failed wording tying the timeout to
        a pull failure — is indistinguishable from a successful setup pull followed
        by an unrelated test timeout, so it must reach the repair agent."""
        failure = CheckFailure(
            name="integration-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "net/http: request canceled while waiting for connection"
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
    def test_awaiting_headers_timeout_without_client_prefix_dispatches_rerun(self) -> None:
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                'Error response from daemon: Get "https://registry-1.docker.io/v2/": '
                "net/http: timeout exceeded while awaiting headers"
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
    def test_registry_timeout_phrase_without_docker_pull_reports_ci_failure(self) -> None:
        """A real app/integration failure that merely logs a net/http timeout
        phrase (no Docker pull / daemon evidence) must reach the repair agent,
        not be silently rerun as transient CI."""
        failure = CheckFailure(
            name="integration-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "calling downstream payments service\n"
                "net/http: request canceled while waiting for connection"
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
    def test_context_deadline_exceeded_without_docker_pull_reports_ci_failure(self) -> None:
        """A bare Go ``context deadline exceeded`` timeout (gRPC / k8s / HTTP
        client) with no Docker pull evidence and no pytest output is a real
        application regression and must reach the repair agent, not be silently
        rerun as transient CI."""
        failure = CheckFailure(
            name="integration-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "calling downstream grpc service\n"
                "rpc error: code = DeadlineExceeded desc = context deadline exceeded"
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
    def test_successful_setup_pull_then_unrelated_test_timeout_reports_ci_failure(
        self,
    ) -> None:
        """A successful setup ``docker pull`` must not license rerunning a real
        integration/Go test that logs ``context deadline exceeded`` many lines
        later in the same ``gh run view --log-failed`` step. The timeout is not
        part of the pull failure, so it must reach the repair agent."""
        failure = CheckFailure(
            name="integration-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "16: Pulling from library/postgres\n"
                "Status: Downloaded newer image for postgres:16\n"
                "=== RUN   TestPaymentsIntegration\n"
                "    payments_test.go:88: calling downstream payments service\n"
                "    payments_test.go:91: context deadline exceeded\n"
                "--- FAIL: TestPaymentsIntegration (30.01s)"
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
    def test_compact_cached_pull_then_test_timeout_at_window_reports_ci_failure(
        self,
    ) -> None:
        """A compact *cached* ``docker pull`` (``Status: Image is up to date``)
        followed by a real Go test ``context deadline exceeded`` sitting exactly
        ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines below the bare ``docker pull``
        echo must still reach the repair agent. The timeout is anchored on Docker
        pull-*failure* wording, not the bare echo, and a cached pull emits no such
        failure line — so there is no Docker evidence to license a rerun."""
        failure = CheckFailure(
            name="integration-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "Status: Image is up to date for postgres:16\n"
                "    payments_test.go:91: context deadline exceeded\n"
                "--- FAIL: TestPaymentsIntegration (30.01s)"
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
    def test_app_failed_to_pull_records_timeout_reports_ci_failure(self) -> None:
        """A real application error whose message merely contains the generic
        ``failed to pull`` phrase (e.g. ``failed to pull records: context
        deadline exceeded``) — with no Docker, daemon, image, or registry
        wording — is not a Docker registry pull failure and must reach the
        repair agent, not be silently rerun as transient CI."""
        failure = CheckFailure(
            name="integration-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "calling downstream records service\n"
                "failed to pull records: context deadline exceeded"
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
    def test_docker_failed_to_pull_image_timeout_dispatches_rerun(self) -> None:
        """A Docker-specific ``failed to pull image`` failure tied to a registry
        timeout is genuine transient infra and is rerun, confirming the marker
        still recognizes real image-pull failures when corroborated by a nearby
        ``docker pull`` command echo."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                'failed to pull image "postgres:16": context deadline exceeded'
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
    def test_unrelated_setup_pull_echo_does_not_corroborate_image_failure(
        self,
    ) -> None:
        """A successful service-container setup ``docker pull postgres:16`` echo
        sitting within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines of a kubelet
        ``Failed to pull image "app"`` event for an *unrelated* application image
        must not corroborate it: the echo targets a different image (and a
        successful setup pull at that), so the real application-image bug must reach
        the repair agent rather than be silently rerun as transient CI."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                "Status: Downloaded newer image for postgres:16\n"
                '    deploy_test.go:51: Failed to pull image "app": '
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
    def test_successful_same_ref_pre_pull_does_not_corroborate_image_failure(
        self,
    ) -> None:
        """A *successful* same-ref ``docker pull ghcr.io/org/app`` pre-pull — proven
        by the ``Status: Image is up to date for ghcr.io/org/app`` success line it
        prints — sitting within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines of a
        kubelet ``Failed to pull image "ghcr.io/org/app"`` event must not corroborate
        it. The echoed pull *succeeded*, so the failure is a separate deploy/image
        bug (a real failure) that must reach the repair agent rather than be silently
        rerun as transient CI. The bare same-ref command echo is not, on its own,
        evidence that the same-ref Docker pull failed."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull ghcr.io/org/app\n"
                "Status: Image is up to date for ghcr.io/org/app\n"
                'Failed to pull image "ghcr.io/org/app": context deadline exceeded'
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
    def test_prefix_ref_success_status_does_not_suppress_same_ref_pull_failure(
        self,
    ) -> None:
        """A success status for a *different* image whose name merely has the failed
        ref as a prefix (``Status: Downloaded newer image for app-db`` vs a failed
        ``app`` pull) must not suppress corroboration of a genuine same-ref ``app``
        pull failure. The success status is matched on the ref as a whitespace token,
        not by substring, so the real transient ``app`` pull failure is still rerun."""
        failure = CheckFailure(
            name="integration-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull app\n"
                "Status: Downloaded newer image for app-db\n"
                'failed to pull image "app": context deadline exceeded'
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
    def test_k8s_failed_to_pull_image_without_docker_context_reports_ci_failure(
        self,
    ) -> None:
        """A bare Kubernetes/containerd kubelet ``Failed to pull image "app"``
        event for an *application* image in an e2e deployment — no ``docker pull``
        echo, daemon error, or registry/``/v2/`` context — is a real image/deploy
        bug, not flaky registry infra. ``failed to pull image`` wording is shared
        with containerd/k8s, so without corroborating Docker pull context it must
        reach the repair agent rather than be silently rerun. (``--- FAIL`` Go
        output is not caught by ``_looks_like_code_failure_text``, so this would
        otherwise be misrouted to ``RerunTransientCI``.)"""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "=== RUN   TestDeployApp\n"
                "    deploy_test.go:42: waiting for app pod to become ready\n"
                '    deploy_test.go:51: Failed to pull image "app": '
                "context deadline exceeded\n"
                "--- FAIL: TestDeployApp (120.00s)"
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
    def test_k8s_registry_qualified_failed_to_pull_image_reports_ci_failure(
        self,
    ) -> None:
        """A registry-*qualified* kubelet/containerd ``Failed to pull image
        "ghcr.io/org/app"`` event must not self-corroborate. The registry host is
        just the image ref's domain, not actual Docker CLI pull context (a same-ref
        ``docker pull`` echo), so the failing application image is a real deploy bug
        that must reach the repair agent rather than be silently rerun as transient
        CI."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "=== RUN   TestDeployApp\n"
                '    deploy_test.go:51: Failed to pull image "ghcr.io/org/app": '
                "context deadline exceeded\n"
                "--- FAIL: TestDeployApp (120.00s)"
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
    def test_k8s_failed_to_pull_image_with_embedded_v2_url_reports_ci_failure(
        self,
    ) -> None:
        """A kubelet/containerd ``Failed to pull image`` event for an *application*
        image embeds the registry transport URL — ``Head "https://ghcr.io/v2/...":
        context deadline exceeded`` — on its own line. The ``/v2/`` is part of the
        kubelet event itself, not separate Docker CLI / registry-protocol evidence,
        so it must not self-corroborate: a ``/v2/`` URL on the failing line (or an
        adjacent wrapped line of the same multi-line event) cannot license a silent
        rerun of a real deploy bug. ``--- FAIL`` Go output is not caught by the
        structured code-failure checks, so without this guard it would be misrouted
        to ``RerunTransientCI`` instead of reaching the repair agent."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "=== RUN   TestDeployApp\n"
                '    deploy_test.go:51: Failed to pull image "ghcr.io/org/app": '
                "rpc error: code = Unknown desc = failed to do request: "
                'Head "https://ghcr.io/v2/org/app/manifests/v1": '
                "context deadline exceeded\n"
                "--- FAIL: TestDeployApp (120.00s)"
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
    def test_k8s_multiline_registry_image_event_reports_ci_failure(self) -> None:
        """A multi-line kubelet event repeats the registry-qualified image ref on
        adjacent lines (``Failed to pull`` then ``Back-off pulling``). A registry
        host on a *neighbouring* ref line is still just the image ref, not Docker
        pull context, so it must not corroborate the ``failed to pull image``
        marker — the application image bug reaches the repair agent rather than
        being silently rerun."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                '  Warning  Failed   kubelet  Failed to pull image "ghcr.io/org/app:v1": '
                "context deadline exceeded\n"
                '  Normal   BackOff  kubelet  Back-off pulling image "ghcr.io/org/app:v1"'
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
    def test_uncorroborated_image_event_timeout_near_transient_pull_reports_ci_failure(
        self,
    ) -> None:
        """A transient service-container ``Docker pull failed`` line (a self-evident
        pull-failure anchor) sitting within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW``
        lines of an *uncorroborated* kubelet ``Failed to pull image "app"`` event
        must not lend its proximity to that event's ``context deadline exceeded``.
        The timeout belongs to the real application-image/deploy bug, not the
        transient pull, so the failure must reach the repair agent rather than be
        silently rerun as transient CI."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "Docker pull failed with exit code 1\n"
                'Failed to pull image "app": context deadline exceeded'
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
    def test_wrapped_uncorroborated_image_event_timeout_near_transient_pull_reports_ci_failure(
        self,
    ) -> None:
        """The wrapped multi-line variant of the uncorroborated-event guard: a
        transient ``Docker pull failed`` anchor, then an *uncorroborated* kubelet
        ``Failed to pull image "app"`` event whose ``context deadline exceeded``
        error is wrapped onto the *next* line. The timeout line carries no ``failed
        to pull image`` text of its own, so an on-line-only guard would attribute it
        to the nearby transient anchor and silently rerun a real deploy bug. The
        timeout belongs to the kubelet event, so the failure must reach the repair
        agent (companion to the same-line
        ``test_uncorroborated_image_event_timeout_near_transient_pull_reports_ci_failure``)."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "Docker pull failed with exit code 1\n"  # evidence index 0
                'Failed to pull image "app"\n'  # uncorroborated index 1, no timeout
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
    def test_app_failed_to_pull_image_phrase_does_not_suppress_transient_pull_timeout(
        self,
    ) -> None:
        """An ordinary application log line that merely *contains* the
        ``failed to pull image`` substring without a quoted ``"<ref>"`` (e.g.
        ``failed to pull image catalog from https://cdn``) is not a
        kubelet/containerd image-pull event, so it must not be treated as an
        uncorroborated pull. A genuine service-container ``Docker pull failed``
        registry timeout within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines of such
        an app line must therefore still be rerun as transient CI, not reported.
        Regression for the loose-substring exclusion that blocked genuine reruns."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "Docker pull failed with exit code 1\n"  # evidence index 0
                "failed to pull image catalog from https://cdn\n"  # index 1 — app line, no ref
                "context deadline exceeded"  # index 2 — registry timeout within window of index 0
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
    def test_uncorroborated_summary_does_not_suppress_self_evident_daemon_timeout(
        self,
    ) -> None:
        """An *uncorroborated* ``Failed to pull image "app"`` summary (no same-ref
        ``docker pull`` echo to corroborate it) sitting immediately before a daemon
        registry-timeout line must not drag the daemon line out of the transient set.
        The daemon line — ``Error response from daemon: Get
        "https://registry-1.docker.io/v2/": context deadline exceeded`` — is *itself*
        self-evident registry-timeout evidence (daemon marker + ``/v2/`` request form +
        timeout marker), so the uncorroborated-event proximity guard must exempt
        timeout lines that are their own pull-failure evidence. Otherwise a real
        registry flake reaches the repair agent instead of being rerun."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                'Failed to pull image "app"\n'  # uncorroborated index 0, no timeout
                'Error response from daemon: Get "https://registry-1.docker.io/v2/": '
                "context deadline exceeded"  # index 1 — self-evident daemon timeout
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
    def test_unrecognized_failure_log_reports_ci_failure(self) -> None:
        """A failure whose log matches neither transient nor registry-timeout
        markers falls through to the repair agent."""
        failure = CheckFailure(
            name="build",
            conclusion="FAILURE",
            log_excerpt="unexpected job termination with no diagnostic output",
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
    def test_registry_timeout_at_exact_window_boundary_dispatches_rerun(self) -> None:
        """A timeout marker exactly ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` (2)
        lines from the pull-failure anchor must still trigger rerun, confirming
        the boundary condition ``abs(index - evidence_index) <= window`` is
        inclusive at the limit. Pins the window so an accidental tightening to 1
        would fail here rather than slip through the closer positive tests."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Docker pull failed with exit code 1\n"  # evidence index 0
                "Retrying pull…\n"  # index 1
                "context deadline exceeded"  # index 2 — distance == 2
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
    def test_docker_pull_with_structured_test_evidence_reports_ci_failure(self) -> None:
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                'Error response from daemon: Get "https://registry-1.docker.io/v2/": '
                "context deadline exceeded (Client.Timeout exceeded while awaiting headers)"
            ),
            run_id="27091023772",
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
    def test_transient_failure_with_code_like_check_name_dispatches_rerun(self) -> None:
        failure = CheckFailure(
            name="TypeCheck / ubuntu-latest",
            conclusion="FAILURE",
            log_excerpt="runner has received a shutdown signal",
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
    def test_timed_out_failure_without_logs_dispatches_rerun(self) -> None:
        failure = CheckFailure(
            name="python-full-coverage",
            conclusion="TIMED_OUT",
            log_excerpt="",
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
    def test_timed_out_failure_without_logs_or_run_id_notifies_human(self) -> None:
        failure = CheckFailure(
            name="python-full-coverage",
            conclusion="TIMED_OUT",
            log_excerpt="",
            run_id=None,
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, NotifyHuman)
        assert action.message is not None
        assert "without actionable code-failure evidence" in action.message

    @pytest.mark.unit
    @pytest.mark.parametrize("conclusion", ["CANCELLED", "ACTION_REQUIRED"])
    def test_transient_non_failed_job_conclusions_notify_human(
        self,
        conclusion: str,
    ) -> None:
        failure = CheckFailure(
            name="python-full-coverage",
            conclusion=conclusion,
            log_excerpt="runner has received a shutdown signal",
            run_id="25655330295",
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
    def test_tool_diagnostics_still_dispatch_agent_repair(self) -> None:
        failure = CheckFailure(
            name="lint-and-type",
            conclusion="FAILURE",
            log_excerpt=(
                "Would reformat: src/awf/runtime/pr_monitor_runner.py\n"
                "src/awf/runtime/pr_monitor.py:12: error: Incompatible types [assignment]"
            ),
            run_id="25655330295",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)
