"""Table-driven tests for ``pr_monitor.decide`` — Docker daemon-error CI failures.

Split out of ``test_pr_monitor_part_003.py`` to keep each test module under the
first-party line-count guardrail.
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
    def test_daemon_error_timeout_without_pull_context_reports_ci_failure(self) -> None:
        """A bare ``Error response from daemon: context deadline exceeded`` from an
        ordinary ``docker run``/``docker build`` test step — no registry URL, image,
        or pull wording — is a real Docker daemon timeout, not a registry image
        pull, so it must reach the repair agent, not be silently rerun. The
        ``error response from daemon`` marker is emitted for any daemon operation,
        so it only anchors a registry timeout with registry/image-pull context."""
        failure = CheckFailure(
            name="integration-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker run --rm postgres:16 pg_isready\n"
                "Error response from daemon: context deadline exceeded"
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
    def test_daemon_error_generic_pulling_from_phrase_reports_ci_failure(self) -> None:
        """A daemon error for a non-registry operation whose text merely contains the
        generic phrase ``pulling from`` (e.g. ``failed while pulling from local
        volume``) must not anchor a nearby timeout as a registry image pull. The
        registry-context markers stay specific to request forms (``/v2/`` and the
        ``auth.docker.io`` token host), so this real daemon timeout reaches the
        repair agent."""
        failure = CheckFailure(
            name="integration-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: failed while pulling from local volume\n"
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
    def test_daemon_error_with_registry_url_timeout_dispatches_rerun(self) -> None:
        """A daemon error whose own line carries registry context (an outbound
        ``/v2/`` registry request) tying the timeout to an image pull is genuine
        transient infra and is still rerun after the marker is narrowed."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                'Error response from daemon: Get "https://registry-1.docker.io/v2/": '
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

    @pytest.mark.unit
    def test_daemon_error_with_docker_hub_auth_endpoint_timeout_dispatches_rerun(
        self,
    ) -> None:
        """Pulling from Docker Hub first fetches a bearer token from
        ``auth.docker.io/token``; when that request times out the daemon reports the
        failure against the auth endpoint rather than ``registry-1.docker.io``/``/v2/``.
        The auth host is the registry-auth token service contacted only for registry
        operations, so it is registry pull context and the timeout is rerun as
        transient infra."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull postgres:16\n"
                'Error response from daemon: Get "https://auth.docker.io/token'
                '?service=registry.docker.io&scope=repository:library/postgres:pull": '
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

    @pytest.mark.unit
    def test_daemon_error_with_ghcr_token_endpoint_timeout_dispatches_rerun(
        self,
    ) -> None:
        """Pulling from a non-Docker-Hub Bearer-auth registry (GHCR, ACR, Harbor,
        ...) first fetches a token from the registry-selected token endpoint —
        ``https://ghcr.io/token?service=...&scope=...``. When that request times out
        the daemon reports the failure against the token endpoint, which carries no
        ``/v2/`` path and is not ``auth.docker.io``. The ``/token?`` request form is
        contacted only for registry auth, so it is registry pull context and the
        timeout is rerun as transient infra rather than reported to the repair
        agent."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull ghcr.io/org/app:v1\n"
                'Error response from daemon: Get "https://ghcr.io/token'
                '?service=ghcr.io&scope=repository:org/app:pull": '
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

    @pytest.mark.unit
    def test_daemon_token_endpoint_denial_does_not_anchor_adjacent_timeout(
        self,
    ) -> None:
        """The ``/token?`` request form, like ``/v2/``, must carry the registry-
        timeout marker on its own line to anchor. A permanent token-auth denial
        (``Error response from daemon: Get "https://ghcr.io/token?...":
        unauthorized``) is a synchronous 401, not a timeout, so an *adjacent
        unrelated* ``context deadline exceeded`` must not let it pose as a transient
        registry pull — the real auth bug must reach the repair agent."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                'Error response from daemon: Get "https://ghcr.io/token'
                '?service=ghcr.io&scope=repository:org/app:pull": unauthorized\n'
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
    def test_daemon_pull_access_denied_does_not_anchor_adjacent_timeout(self) -> None:
        """``pull access denied`` is a synchronous registry 403 — it cannot itself
        cause a ``context deadline exceeded``, so a daemon-error auth-denial line
        must not anchor an *adjacent* unrelated timeout. A real auth-config bug
        (``Error response from daemon: pull access denied for myapp``) sitting one
        line away from an unrelated health-check/retry timeout must reach the repair
        agent rather than be silently rerun as transient infra."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: pull access denied for myapp\n"
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
    def test_daemon_registry_qualified_pull_access_denied_does_not_anchor_timeout(
        self,
    ) -> None:
        """A registry-qualified permanent daemon error must not anchor an adjacent
        timeout. ``Error response from daemon: pull access denied for
        ghcr.io/org/app`` is a synchronous 403 (the registry host sits on the image
        *ref*, not on an outbound request), so a nearby unrelated ``context deadline
        exceeded`` must not let it pose as a transient registry pull. The auth bug
        must reach the repair agent rather than be silently rerun: a bare image-
        reference host is not a registry-*request* form."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: pull access denied for ghcr.io/org/app\n"
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
    def test_daemon_registry_qualified_no_such_image_does_not_anchor_timeout(
        self,
    ) -> None:
        """``Error response from daemon: No such image: ghcr.io/org/app`` is a
        permanent missing-image error, not a registry timeout — the host is on the
        image ref, not an outbound ``/v2/`` request. A nearby unrelated ``context
        deadline exceeded`` must not anchor it as a transient pull; the real
        image/config bug must reach the repair agent."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: No such image: ghcr.io/org/app:latest\n"
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
    def test_daemon_registry_request_denial_does_not_anchor_adjacent_timeout(
        self,
    ) -> None:
        """A permanent daemon error can quote the ``/v2/`` registry *request* form yet
        still be a synchronous auth denial, not a timeout — ``Error response from
        daemon: Head "https://ghcr.io/v2/org/app/manifests/latest": denied``. The
        ``/v2/`` request form alone must not anchor an *adjacent unrelated* ``context
        deadline exceeded``; only a daemon line carrying the registry-timeout marker
        itself is transient infra, so this real auth bug must reach the repair
        agent rather than be silently rerun."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: Head "
                '"https://ghcr.io/v2/org/app/manifests/latest": denied\n'
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
    def test_bare_daemon_error_does_not_anchor_failed_to_pull_image(self) -> None:
        """A bare ``Error response from daemon: context deadline exceeded`` line —
        no registry/image-pull context — must not corroborate a nearby
        kubelet-style ``Failed to pull image "app"`` event. Both are real failures
        (a ``docker run`` daemon timeout and an application image/deploy bug), so
        the daemon line is not Docker pull context and the failure must reach the
        repair agent rather than be silently rerun as transient CI."""
        failure = CheckFailure(
            name="e2e-tests",
            conclusion="FAILURE",
            log_excerpt=(
                "Error response from daemon: context deadline exceeded\n"
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
