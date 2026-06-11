"""Unit tests for ``_image_ref_matches_daemon_url``.

Split out of ``test_pr_monitor_part_005.py`` to keep each test module under
the first-party line-count guardrail.
"""

from __future__ import annotations

import pytest

from awf.runtime._docker_pull_detection import (
    _evidence_line_is_permanent_pull_failure,
    _forward_detail_ref_matches_pull,
    _image_ref_matches_daemon_url,
    _strip_image_tag,
)
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    PRStatus,
    ReportCiFailure,
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


class TestImageRefMatchesDaemonUrl:
    """Unit tests for ``_image_ref_matches_daemon_url``."""

    @pytest.mark.unit
    def test_private_registry_with_port_matches_daemon_url(self) -> None:
        """A private-registry image ref with an explicit port must match the
        corresponding daemon distribution-API URL.

        Before the fix, ``partition(":")`` split on the port colon, yielding
        ``ref_no_tag = "registry.example.com"`` (a hostname) instead of
        ``registry.example.com:8080/myapp``.  The ``/v2/myapp/`` fragment check
        then failed and the pull was misclassified as transient.

        Regression for issue:4644047856."""
        line = 'Head "https://registry.example.com:8080/v2/myapp/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("registry.example.com:8080/myapp:latest", line) is True

    @pytest.mark.unit
    def test_standard_registry_image_with_tag_matches_daemon_url(self) -> None:
        """A normal registry image ref (no port) continues to match correctly."""
        line = 'Head "https://ghcr.io/v2/org/app/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", line) is True

    @pytest.mark.unit
    def test_private_registry_with_port_and_decide_reports_ci_failure(self) -> None:
        """``decide()`` must classify a denied pull for a private-registry-with-port
        image as a permanent CI failure (ReportCiFailure), not a transient rerun.

        Regression for issue:4644047856."""
        failure = CheckFailure(
            name="python-coverage-shards (1)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull registry.example.com:8080/myapp:latest\n"
                "Docker pull failed with exit code 1\n"
                'Error response from daemon: Head "https://registry.example.com:8080/v2/myapp/manifests/latest": denied\n'
                "context deadline exceeded"
            ),
            run_id="99000000001",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_digest_pinned_ref_matches_daemon_url(self) -> None:
        """A digest-pinned image ref (``@sha256:...``) must match the daemon URL.

        Before the fix, ``rfind(":")`` found the colon inside ``sha256:bad``,
        leaving ``ref_no_tag = ghcr.io/org/app@sha256`` and therefore
        ``repo_path = org/app@sha256``.  The ``/v2/org/app/`` fragment check
        then failed and the pull was misclassified as transient.

        Regression for PRRT_kwDOSJAM6s6HtAGv."""
        line = 'Head "https://ghcr.io/v2/org/app/manifests/sha256:bad": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app@sha256:bad", line) is True

    @pytest.mark.unit
    def test_digest_pinned_ref_no_registry_matches_daemon_url(self) -> None:
        """A digest-pinned official (library) image must resolve to
        ``/v2/library/<name>/`` even when the ref uses a digest, not a tag."""
        line = (
            'Head "https://registry-1.docker.io/v2/library/postgres/manifests/sha256:abc": denied'
        )
        assert _image_ref_matches_daemon_url("postgres@sha256:abc", line) is True

    @pytest.mark.unit
    def test_localhost_port_registry_matches_daemon_url(self) -> None:
        """A local registry ref (localhost:5000/myapp:latest) must match the daemon
        distribution-API URL.

        Before the fix, ``"." in ref_no_tag[:slash]`` evaluated False for
        ``localhost:5000`` (no dot), so ``repo_path`` was set to
        ``localhost:5000/myapp`` and the ``/v2/myapp/`` fragment check failed,
        causing the pull to be misclassified as transient.

        Regression for PRRT_kwDOSJAM6s6HtAGx."""
        line = 'Head "http://localhost:5000/v2/myapp/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("localhost:5000/myapp:latest", line) is True

    @pytest.mark.unit
    def test_bare_localhost_registry_matches_daemon_url(self) -> None:
        """A bare ``localhost/myapp:latest`` ref (no port) must also match."""
        line = 'Head "http://localhost/v2/myapp/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("localhost/myapp:latest", line) is True

    @pytest.mark.unit
    def test_different_tag_same_repo_does_not_match_daemon_url(self) -> None:
        """A daemon error for a different tag of the same repository must NOT
        match the preceding pull image ref.

        When ``docker pull ghcr.io/org/app:good`` precedes a daemon error
        ``Head "https://ghcr.io/v2/org/app/manifests/bad": denied``, the two
        refer to different manifests and must not be conflated.  Without the
        tag/digest check, the repo-only fragment ``/v2/org/app/`` appears in
        both URLs and the mismatch goes undetected, causing AWF to skip a
        legitimate rerun.

        Regression for PRRT_kwDOSJAM6s6HtGA_."""
        line = 'Head "https://ghcr.io/v2/org/app/manifests/bad": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:good", line) is False

    @pytest.mark.unit
    def test_same_tag_same_repo_matches_daemon_url(self) -> None:
        """A daemon error for the same tag of the same repository must match.

        Counterpart to the different-tag regression: ensure the fix does not
        over-reject same-repo, same-tag daemon errors.

        Regression for PRRT_kwDOSJAM6s6HtGA_."""
        line = 'Head "https://ghcr.io/v2/org/app/manifests/good": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:good", line) is True

    @pytest.mark.unit
    def test_untagged_pull_matches_latest_manifest_daemon_url(self) -> None:
        """An untagged image ref (e.g. ``ghcr.io/org/app``) must only match daemon
        URLs for ``:latest`` — not for an arbitrary tag.

        Docker treats an untagged pull as ``:latest``.  Before the fix, the
        missing ``manifest_ref`` caused the code to return True for any
        ``/manifests/<tag>`` URL of the same repo, so a permanent daemon denial
        for ``.../manifests/bad`` could be falsely attributed to the untagged
        pull and prevent a legitimate transient-timeout rerun.

        Regression for PRRT_kwDOSJAM6s6HtImz."""
        latest_line = 'Head "https://ghcr.io/v2/org/app/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app", latest_line) is True

        bad_line = 'Head "https://ghcr.io/v2/org/app/manifests/bad": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app", bad_line) is False

    @pytest.mark.unit
    def test_untagged_library_pull_matches_latest_manifest_daemon_url(self) -> None:
        """Same as above but for Docker Hub library (single-component) images.

        An untagged ``postgres`` pull defaults to ``:latest`` and must only match
        daemon URLs for ``/v2/library/postgres/manifests/latest``.

        Regression for PRRT_kwDOSJAM6s6HtImz."""
        latest_line = (
            'Head "https://registry-1.docker.io/v2/library/postgres/manifests/latest": denied'
        )
        assert _image_ref_matches_daemon_url("postgres", latest_line) is True

        bad_line = 'Head "https://registry-1.docker.io/v2/library/postgres/manifests/bad": denied'
        assert _image_ref_matches_daemon_url("postgres", bad_line) is False

    @pytest.mark.unit
    def test_implicit_docker_hub_library_image_wrong_registry_does_not_match(self) -> None:
        """An unqualified Docker Hub library image ref must NOT match a daemon
        error URL from a different registry that embeds the same library path.

        When ``docker pull postgres:16`` is an implicit Docker Hub pull and an
        unrelated permanent error ``Head
        "https://ghcr.io/v2/library/postgres/manifests/16": denied`` appears in
        the same log window, the helper must return False — the daemon URL belongs
        to ghcr.io, not Docker Hub, and must not suppress a legitimate
        transient-timeout rerun.

        Without the Docker Hub host guard the hostless
        ``/v2/library/postgres/manifests/16`` fragment matches the ghcr.io URL as
        a substring, causing _log_shows_docker_registry_timeout() to treat the
        transient Docker Hub timeout as permanent and skip the rerun.

        Regression for PRRT_kwDOSJAM6s6HtQiD."""
        ghcr_line = 'Head "https://ghcr.io/v2/library/postgres/manifests/16": denied'
        assert _image_ref_matches_daemon_url("postgres:16", ghcr_line) is False

        other_line = 'Head "https://registry.example.com/v2/library/postgres/manifests/16": denied'
        assert _image_ref_matches_daemon_url("postgres:16", other_line) is False

    @pytest.mark.unit
    def test_implicit_docker_hub_library_image_matches_docker_hub_daemon_url(self) -> None:
        """An unqualified Docker Hub library image ref must match a daemon error
        URL from the Docker Hub registry.

        Counterpart to the cross-registry false-positive regression: a permanent
        denial from ``registry-1.docker.io`` for ``postgres:16`` must still be
        attributed to the same pull and suppress a rerun.

        Regression for PRRT_kwDOSJAM6s6HtQiD."""
        hub_line = 'Head "https://registry-1.docker.io/v2/library/postgres/manifests/16": denied'
        assert _image_ref_matches_daemon_url("postgres:16", hub_line) is True

    @pytest.mark.unit
    def test_different_registry_same_repo_and_tag_does_not_match_daemon_url(self) -> None:
        """A daemon error from a different registry must NOT match the preceding
        pull image ref even when the repo path and tag are identical.

        When ``docker pull ghcr.io/org/app:latest`` times out and an unrelated
        permanent daemon error ``Head
        "https://registry.example.com/v2/org/app/manifests/latest": denied``
        appears in the same log window, the helper must return False — the daemon
        URL belongs to a different registry and must not anchor transient-timeout
        attribution for the ghcr.io pull.

        Without the registry-host check only the ``/v2/org/app/manifests/latest``
        fragment is compared, which matches both registries and causes AWF to
        report a permanent CI failure instead of scheduling a rerun.

        Regression for PRRT_kwDOSJAM6s6HtKzI."""
        line = 'Head "https://registry.example.com/v2/org/app/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", line) is False

    @pytest.mark.unit
    def test_correct_registry_same_repo_and_tag_still_matches_daemon_url(self) -> None:
        """Counterpart to the different-registry regression: a daemon error from
        the *same* registry must still match.

        Regression for PRRT_kwDOSJAM6s6HtKzI."""
        line = 'Head "https://ghcr.io/v2/org/app/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", line) is True

    @pytest.mark.unit
    def test_implicit_docker_hub_org_image_wrong_registry_does_not_match(self) -> None:
        """An unqualified Docker Hub user/org image ref must NOT match a daemon
        error URL from a different registry with the same repo path and tag.

        When ``docker pull org/app:latest`` is an implicit Docker Hub pull and an
        unrelated permanent error ``Head "https://ghcr.io/v2/org/app/manifests/latest":
        denied`` appears in the same log window, the helper must return False — the
        daemon URL belongs to ghcr.io, not Docker Hub, and must not suppress a
        legitimate transient-timeout rerun.

        Without the Docker Hub host guard the hostless ``/v2/org/app/manifests/latest``
        fragment matches the ghcr.io URL as a substring, causing AWF to report a
        permanent CI failure instead of scheduling a rerun.

        Regression for PRRT_kwDOSJAM6s6HtNI4."""
        ghcr_line = 'Head "https://ghcr.io/v2/org/app/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("org/app:latest", ghcr_line) is False

        other_line = 'Head "https://registry.example.com/v2/org/app/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("org/app:latest", other_line) is False

    @pytest.mark.unit
    def test_implicit_docker_hub_org_image_matches_docker_hub_daemon_url(self) -> None:
        """An unqualified Docker Hub user/org image ref must match a daemon error
        URL from the Docker Hub registry.

        Counterpart to the cross-registry false-positive regression: a permanent
        denial from ``registry-1.docker.io`` for ``org/app:latest`` must still
        be attributed to the same pull and suppress a rerun.

        Regression for PRRT_kwDOSJAM6s6HtNI4."""
        hub_line = 'Head "https://registry-1.docker.io/v2/org/app/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("org/app:latest", hub_line) is True

    @pytest.mark.unit
    def test_token_endpoint_auth_failure_matches_registry_image(self) -> None:
        """A permanent auth failure at the token service endpoint must match the
        in-flight image ref.

        When Docker pulls ``ghcr.io/org/app:latest`` and the registry token
        service returns a denial — ``Get
        "https://ghcr.io/token?scope=repository%3aorg%2fapp%3apull": denied``
        — the daemon error carries no ``/v2/manifests/`` path.  The URL-encoded
        scope fragment ``scope=repository%3aorg%2fapp%3a`` must be recognised
        so the permanence probe can attribute the error to the in-flight pull.

        Lines are lowercased before being passed to this helper (see
        ``_log_shows_docker_registry_timeout``), so percent-encoding digits
        arrive as lowercase (%3a, %2f).

        Regression for PRRT_kwDOSJAM6s6HtSvu."""
        line = 'get "https://ghcr.io/token?scope=repository%3aorg%2fapp%3apull": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", line) is True

    @pytest.mark.unit
    def test_token_endpoint_auth_failure_wrong_registry_does_not_match(self) -> None:
        """A token-service denial from a *different* registry must NOT match the
        in-flight image ref even when the repo path is identical.

        A permanent denial ``Get
        "https://registry.example.com/token?scope=repository%3aorg%2fapp%3apull":
        denied`` must not be attributed to a ``ghcr.io/org/app:latest`` pull —
        the registry host does not match.

        Regression for PRRT_kwDOSJAM6s6HtSvu."""
        line = (
            'get "https://registry.example.com/token?scope=repository%3aorg%2fapp%3apull": denied'
        )
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", line) is False

    @pytest.mark.unit
    def test_docker_hub_library_image_token_endpoint_matches(self) -> None:
        """An unqualified Docker Hub library image must match a token-service
        denial that uses the ``library/<name>`` scope encoding.

        Docker Hub token requests encode a bare image name like ``postgres``
        as ``scope=repository%3alibrary%2fpostgres%3apull``.  The helper must
        recognise this encoding so a permanent auth failure at the Docker Hub
        token service suppresses a rerun.

        Regression for PRRT_kwDOSJAM6s6HtSvu."""
        line = 'get "https://auth.docker.io/token?scope=repository%3alibrary%2fpostgres%3apull": denied'
        assert _image_ref_matches_daemon_url("postgres:16", line) is True

    @pytest.mark.unit
    def test_docker_hub_library_image_token_endpoint_wrong_registry_does_not_match(self) -> None:
        """An unqualified Docker Hub library image must NOT match a token denial
        from a different registry that happens to embed the same library scope.

        Regression for PRRT_kwDOSJAM6s6HtSvu."""
        line = 'get "https://ghcr.io/token?scope=repository%3alibrary%2fpostgres%3apull": denied'
        assert _image_ref_matches_daemon_url("postgres:16", line) is False

    @pytest.mark.unit
    def test_explicit_non_docker_hub_library_path_ref_does_not_match_docker_hub_manifest_url(
        self,
    ) -> None:
        """An explicit non-Docker-Hub ref with a single-component path must NOT
        match a Docker Hub library manifest URL.

        ``docker pull ghcr.io/postgres:16`` resolves to a single-component
        repo path (``postgres``) with an explicit non-Docker-Hub host.  When a
        nearby unrelated daemon denial for
        ``https://registry-1.docker.io/v2/library/postgres/manifests/16``
        appears in the same log window, ``_image_ref_matches_daemon_url`` must
        return False — the library/ URL belongs to Docker Hub, not ghcr.io,
        and must not be attributed to the ghcr.io pull.

        Without the Docker Hub host guard on the library/ block the single-
        component path triggers the library fallback, the hostless
        ``/v2/library/postgres/manifests/16`` fragment matches the Docker Hub
        URL, and AWF falsely suppresses a legitimate transient-timeout rerun.

        Regression for PRRT_kwDOSJAM6s6HtWjC."""
        hub_line = 'Head "https://registry-1.docker.io/v2/library/postgres/manifests/16": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/postgres:16", hub_line) is False

        other_line = 'Head "https://registry.example.com/v2/library/postgres/manifests/16": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/postgres:16", other_line) is False

    @pytest.mark.unit
    def test_explicit_non_docker_hub_library_path_ref_does_not_match_docker_hub_token_url(
        self,
    ) -> None:
        """An explicit non-Docker-Hub ref with a single-component path must NOT
        match a Docker Hub library token-service denial.

        ``docker pull ghcr.io/postgres:16`` must not be attributed to an
        ``auth.docker.io`` token denial that embeds
        ``scope=repository%3alibrary%2fpostgres%3a``.  That token endpoint
        belongs to Docker Hub, not ghcr.io.

        Regression for PRRT_kwDOSJAM6s6HtWjC."""
        line = (
            'get "https://auth.docker.io/token?scope=repository%3alibrary%2fpostgres%3apull":'
            " denied"
        )
        assert _image_ref_matches_daemon_url("ghcr.io/postgres:16", line) is False

    @pytest.mark.unit
    def test_token_endpoint_permanent_auth_failure_reports_ci_failure(self) -> None:
        """``decide()`` must classify a token-service auth denial as a permanent
        CI failure (ReportCiFailure), not a transient rerun.

        When Docker encounters ``Get
        "https://ghcr.io/token?scope=repository%3Aorg%2Fapp%3Apull": denied``
        followed by ``docker pull failed`` and an adjacent ``context deadline
        exceeded``, the preceding token-service denial is permanent and must
        suppress the transient-timeout classification.

        Before the fix, ``_image_ref_matches_daemon_url`` did not recognise
        token-service URLs, so the permanence probe failed to attribute the
        denial to the in-flight pull, and AWF burned a rerun.

        Regression for PRRT_kwDOSJAM6s6HtSvu."""
        failure = CheckFailure(
            name="python-coverage-shards (1)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull ghcr.io/org/app:latest\n"
                'Error response from daemon: Get "https://ghcr.io/token?scope=repository%3Aorg%2Fapp%3Apull": denied\n'
                "docker pull failed with exit code 1\n"
                "context deadline exceeded"
            ),
            run_id="99000000002",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_explicit_docker_io_library_image_matches_docker_hub_manifest_url(self) -> None:
        """A ``docker.io/``-prefixed Docker Hub library image must match a daemon
        error URL from ``registry-1.docker.io``.

        ``docker pull docker.io/postgres:16`` is an explicit Docker Hub pull.
        Docker resolves the omitted namespace to the library namespace, so the
        daemon reports ``registry-1.docker.io/v2/library/postgres/manifests/16``.
        Before the fix, ``docker.io`` was absent from ``_DOCKER_HUB_REGISTRY_HOSTS``
        so the library-URL guard skipped the ``/v2/library/<name>/`` check entirely
        and the function returned False — the permanent denial was not attributed
        to the pull, and AWF reran as transient instead of reporting the failure.

        Regression for PRRT_kwDOSJAM6s6HtZne."""
        hub_line = 'Head "https://registry-1.docker.io/v2/library/postgres/manifests/16": denied'
        assert _image_ref_matches_daemon_url("docker.io/postgres:16", hub_line) is True

    @pytest.mark.unit
    def test_explicit_docker_io_library_image_wrong_registry_does_not_match(self) -> None:
        """A ``docker.io/``-prefixed Docker Hub library image must NOT match a
        daemon error URL from a different registry.

        Counterpart to the positive regression: a permanent denial from ghcr.io
        that embeds ``/v2/library/postgres/manifests/16`` must not be attributed
        to a ``docker.io/postgres:16`` pull.

        Regression for PRRT_kwDOSJAM6s6HtZne."""
        ghcr_line = 'Head "https://ghcr.io/v2/library/postgres/manifests/16": denied'
        assert _image_ref_matches_daemon_url("docker.io/postgres:16", ghcr_line) is False

    @pytest.mark.unit
    def test_explicit_docker_io_library_image_token_endpoint_matches(self) -> None:
        """A ``docker.io/``-prefixed Docker Hub library image must match an
        ``auth.docker.io`` token-service denial with the ``library/<name>`` scope.

        Regression for PRRT_kwDOSJAM6s6HtZne."""
        line = (
            'get "https://auth.docker.io/token?scope=repository%3alibrary%2fpostgres%3apull":'
            " denied"
        )
        assert _image_ref_matches_daemon_url("docker.io/postgres:16", line) is True

    @pytest.mark.unit
    def test_explicit_docker_io_library_image_permanent_failure_reports_ci_failure(
        self,
    ) -> None:
        """``decide()`` must classify a permanent denial for a ``docker.io/``-
        prefixed Docker Hub library image as a non-retryable CI failure.

        Before the fix, ``_image_ref_matches_daemon_url`` returned False for
        ``docker.io/postgres:16`` daemon URLs (``docker.io`` missing from
        ``_DOCKER_HUB_REGISTRY_HOSTS``), so the permanence probe could not
        attribute the denial to the pull, leaving it in the transient set and
        producing RerunTransientCI instead of ReportCiFailure.

        Regression for PRRT_kwDOSJAM6s6HtZne."""
        failure = CheckFailure(
            name="python-coverage-shards (1)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull docker.io/postgres:16\n"
                'Error response from daemon: Head "https://registry-1.docker.io/v2/library/postgres/manifests/16": denied\n'
                "docker pull failed with exit code 1\n"
                "context deadline exceeded"
            ),
            run_id="99000000003",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_hostname_suffix_does_not_match_longer_registry_host(self) -> None:
        """A pull from ``registry.example.com`` must not match a daemon error URL
        from ``prod.registry.example.com`` just because the former hostname is a
        raw substring of the latter.

        Without URL-boundary anchoring the prefix
        ``registry.example.com/v2/org/app/manifests/`` is a substring of
        ``prod.registry.example.com/v2/org/app/manifests/``, causing an unrelated
        transient timeout to be mis-classified as a permanent denial and the
        rerun to be skipped.

        Regression for PRRT_kwDOSJAM6s6HtZng."""
        # Line comes from a *different* registry (prod.registry.example.com)
        unrelated_line = (
            'Head "https://prod.registry.example.com/v2/org/app/manifests/latest": denied'
        )
        assert (
            _image_ref_matches_daemon_url("registry.example.com/org/app:latest", unrelated_line)
            is False
        )

        # Sanity: the pull's own registry host still matches
        own_line = 'Head "https://registry.example.com/v2/org/app/manifests/latest": denied'
        assert (
            _image_ref_matches_daemon_url("registry.example.com/org/app:latest", own_line) is True
        )

    @pytest.mark.unit
    def test_token_endpoint_hostname_suffix_does_not_match_longer_registry_host(
        self,
    ) -> None:
        """A token-service URL from a longer host that shares the pull-image host
        as a suffix must NOT be attributed to the pull.

        When ``docker pull registry.example.com/org/app:latest`` is in flight and
        a token-service denial from
        ``https://prod.registry.example.com/token?scope=repository%3aorg%2fapp%3apull``
        appears in the log window, the raw-substring ``_host in line`` check
        returns True because ``registry.example.com`` is contained inside
        ``prod.registry.example.com``.  The token URL host must be compared at
        URL-boundary granularity ("//<host>/") so the unrelated denial does not
        suppress a legitimate transient-timeout rerun.

        Regression for PRRT_kwDOSJAM6s6HtfLR."""
        unrelated_line = (
            'get "https://prod.registry.example.com/token?'
            'scope=repository%3aorg%2fapp%3apull": denied'
        )
        assert (
            _image_ref_matches_daemon_url("registry.example.com/org/app:latest", unrelated_line)
            is False
        )

        # Sanity: the pull's own registry token URL still matches
        own_line = (
            'get "https://registry.example.com/token?scope=repository%3aorg%2fapp%3apull": denied'
        )
        assert (
            _image_ref_matches_daemon_url("registry.example.com/org/app:latest", own_line) is True
        )

    @pytest.mark.unit
    def test_explicit_docker_io_org_image_matches_docker_hub_manifest_url(self) -> None:
        """A ``docker.io/``-prefixed non-library Docker Hub image must match a
        daemon error URL from ``registry-1.docker.io``.

        ``docker pull docker.io/org/app:bad`` is an explicit Docker Hub pull.
        Docker resolves the alias to ``registry-1.docker.io``, so the daemon
        error reports
        ``https://registry-1.docker.io/v2/org/app/manifests/bad``.  Before the
        fix the code built ``//docker.io/v2/org/app/manifests/`` as the scoped
        prefix, which is not a substring of the daemon URL, so the function
        returned False — the permanent denial was not attributed to the pull and
        AWF burned a rerun.

        Regression for PRRT_kwDOSJAM6s6Htl06."""
        hub_line = 'Head "https://registry-1.docker.io/v2/org/app/manifests/bad": denied'
        assert _image_ref_matches_daemon_url("docker.io/org/app:bad", hub_line) is True

    @pytest.mark.unit
    def test_explicit_docker_io_org_image_wrong_registry_does_not_match(self) -> None:
        """A ``docker.io/``-prefixed non-library Docker Hub image must NOT match
        a daemon error URL from a different registry.

        Counterpart to the positive regression: a permanent denial from ghcr.io
        for the same path must not be attributed to a ``docker.io/org/app:bad``
        pull.

        Regression for PRRT_kwDOSJAM6s6Htl06."""
        ghcr_line = 'Head "https://ghcr.io/v2/org/app/manifests/bad": denied'
        assert _image_ref_matches_daemon_url("docker.io/org/app:bad", ghcr_line) is False

        other_line = 'Head "https://registry.example.com/v2/org/app/manifests/bad": denied'
        assert _image_ref_matches_daemon_url("docker.io/org/app:bad", other_line) is False

    @pytest.mark.unit
    def test_explicit_docker_io_org_image_permanent_failure_reports_ci_failure(
        self,
    ) -> None:
        """``decide()`` must classify a permanent denial for a ``docker.io/``-
        prefixed non-library Docker Hub image as a non-retryable CI failure.

        Before the fix the permanence probe could not attribute the daemon URL
        to the pull (host mismatch: alias vs resolved host), leaving the error
        in the transient set and producing RerunTransientCI instead of
        ReportCiFailure.

        Regression for PRRT_kwDOSJAM6s6Htl06."""
        failure = CheckFailure(
            name="python-coverage-shards (1)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull docker.io/org/app:bad\n"
                'Error response from daemon: Head "https://registry-1.docker.io/v2/org/app/manifests/bad": denied\n'
                "docker pull failed with exit code 1\n"
                "context deadline exceeded"
            ),
            run_id="99000000004",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_token_endpoint_auth_failure_unencoded_scope_matches_registry_image(self) -> None:
        """A permanent token-service denial with an *unencoded* scope parameter
        must still be attributed to the in-flight image ref.

        Docker daemons sometimes emit the OAuth scope unencoded —
        ``scope=repository:org/app:pull`` — rather than in the percent-encoded
        form ``scope=repository%3aorg%2fapp%3a``.  The helper must match both
        forms so that a permanent auth failure from ``ghcr.io`` is correctly
        identified and suppresses an erroneous rerun.

        Regression for PRRT_kwDOSJAM6s6HuFH6."""
        line = 'get "https://ghcr.io/token?service=ghcr.io&scope=repository:org/app:pull": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", line) is True

    @pytest.mark.unit
    def test_token_endpoint_auth_failure_unencoded_scope_wrong_registry_does_not_match(
        self,
    ) -> None:
        """An unencoded-scope token denial from a *different* registry must NOT
        match the in-flight pull.

        Counterpart to the positive regression: the host-boundary guard must
        still apply when the scope is unencoded.

        Regression for PRRT_kwDOSJAM6s6HuFH6."""
        line = (
            'get "https://other.registry.io/token?service=other.registry.io'
            '&scope=repository:org/app:pull": denied'
        )
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", line) is False

    @pytest.mark.unit
    def test_docker_hub_host_alias_substring_in_unrelated_registry_does_not_match(
        self,
    ) -> None:
        """A daemon error URL from a registry whose hostname merely *contains* a
        Docker Hub alias as a suffix must NOT match an unqualified Docker Hub
        image ref.

        ``docker pull org/app:latest`` is an implicit Docker Hub pull.  When a
        nearby permanent daemon error from ``evil-docker.io`` appears in the same
        log window, the bare substring check ``"docker.io" in line`` returns True
        because ``"docker.io"`` is a suffix of ``"evil-docker.io"``.  The Docker
        Hub host guard must use URL-host boundary parsing (``"//<host>/"`` rather
        than a bare substring) so the unrelated denial does not suppress a
        legitimate transient-timeout rerun.

        Regression for PRRT_kwDOSJAM6s6HubRp."""
        evil_line = 'head "https://evil-docker.io/v2/org/app/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("org/app:latest", evil_line) is False

        # Sanity: a real Docker Hub URL still matches.
        hub_line = 'head "https://registry-1.docker.io/v2/org/app/manifests/latest": denied'
        assert _image_ref_matches_daemon_url("org/app:latest", hub_line) is True

    @pytest.mark.unit
    def test_docker_hub_library_image_token_endpoint_unencoded_scope_matches(self) -> None:
        """An unqualified Docker Hub library image must match a token-service
        denial that uses the *unencoded* ``library/<name>`` scope form.

        Regression for PRRT_kwDOSJAM6s6HuFH6."""
        line = (
            'get "https://auth.docker.io/token'
            '?service=registry.docker.io&scope=repository:library/postgres:pull": denied'
        )
        assert _image_ref_matches_daemon_url("postgres:16", line) is True

    @pytest.mark.unit
    def test_docker_hub_library_image_token_endpoint_unencoded_scope_wrong_registry_does_not_match(
        self,
    ) -> None:
        """An unencoded-scope Docker Hub library token denial from a non-Docker-Hub
        registry must NOT match an unqualified Docker Hub library pull.

        Regression for PRRT_kwDOSJAM6s6HuFH6."""
        line = 'get "https://ghcr.io/token?scope=repository:library/postgres:pull": denied'
        assert _image_ref_matches_daemon_url("postgres:16", line) is False

    @pytest.mark.unit
    def test_tag_prefix_does_not_match_longer_tag(self) -> None:
        """A pulled tag that is a prefix of another tag for the same repo must
        NOT be attributed to a daemon URL for the longer tag.

        With ``docker pull ghcr.io/org/app:v1`` in flight, a permanent denial
        for ``https://ghcr.io/v2/org/app/manifests/v10`` must not match — the
        raw substring ``/manifests/v1`` is contained in ``/manifests/v10``, so a
        boundary check after the ref is required.

        Regression for PRRT_kwDOSJAM6s6HuMAx."""
        longer_tag_line = 'Head "https://ghcr.io/v2/org/app/manifests/v10": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:v1", longer_tag_line) is False

        own_line = 'Head "https://ghcr.io/v2/org/app/manifests/v1": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:v1", own_line) is True

    @pytest.mark.unit
    def test_library_tag_prefix_does_not_match_longer_tag(self) -> None:
        """For Docker Hub library images a pulled tag that is a prefix of another
        tag must NOT be attributed to a daemon URL for the longer tag.

        With ``docker pull postgres:v1`` in flight, a permanent denial for
        ``registry-1.docker.io/v2/library/postgres/manifests/v10`` must not
        match — ``/manifests/v1`` is a substring of ``/manifests/v10``, so the
        library manifest branch requires the same URL-boundary check as the
        non-library branch.

        Regression for PRRT_kwDOSJAM6s6HubRs."""
        longer_tag_line = (
            'Head "https://registry-1.docker.io/v2/library/postgres/manifests/v10": denied'
        )
        assert _image_ref_matches_daemon_url("postgres:v1", longer_tag_line) is False

        own_line = 'Head "https://registry-1.docker.io/v2/library/postgres/manifests/v1": denied'
        assert _image_ref_matches_daemon_url("postgres:v1", own_line) is True

    @pytest.mark.unit
    def test_unencoded_token_scope_permanent_denial_with_pull_echo_reports_ci_failure(
        self,
    ) -> None:
        """``decide()`` must classify a permanent token-service denial with an
        unencoded scope as a non-retryable CI failure even when a pull echo is
        present.

        Log order: pull echo → unencoded-scope token denial → adjacent
        ``context deadline exceeded``.  Without the unencoded-scope fix
        ``_image_ref_matches_daemon_url`` returns False (it only matched the
        percent-encoded form), the permanence probe cannot attribute the denial
        to the pull, and AWF incorrectly returns RerunTransientCI.

        Regression for PRRT_kwDOSJAM6s6HuFH6."""
        failure = CheckFailure(
            name="python-coverage-shards (2)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull ghcr.io/org/app:v1\n"
                'Error response from daemon: Get "https://ghcr.io/token'
                '?service=ghcr.io&scope=repository:org/app:pull": denied\n'
                "context deadline exceeded"
            ),
            run_id="99000001005",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_non_manifest_blob_url_same_repo_does_not_match(self) -> None:
        """A daemon error URL for a non-manifest path (blob) of the same repo must
        NOT be attributed to the in-flight pull.

        Blob URLs (``/v2/<repo>/blobs/<digest>``) are content-addressed and carry
        no tag, so the same repository path appears for blobs of *any* tag.  A
        permanent ``not found`` blob error for a different image/tag must not
        suppress a legitimate transient-timeout rerun by matching solely on the
        ``/v2/<repo>/`` prefix.

        Regression for PRRT_kwDOSJAM6s6Huzsy."""
        blob_line = 'get "https://ghcr.io/v2/org/app/blobs/sha256:bad": not found'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:good", blob_line) is False

    @pytest.mark.unit
    def test_library_non_manifest_blob_url_does_not_match(self) -> None:
        """A daemon error URL for a non-manifest blob path of a Docker Hub library
        image must NOT be attributed to the in-flight pull.

        The same content-addressed blob path applies regardless of tag, so
        ``/v2/library/<name>/blobs/<digest>`` must not match a pull of
        ``<name>:<tag>`` without manifest-level ref verification.

        Regression for PRRT_kwDOSJAM6s6Huzsy."""
        blob_line = (
            'get "https://registry-1.docker.io/v2/library/postgres/blobs/sha256:bad": not found'
        )
        assert _image_ref_matches_daemon_url("postgres:16", blob_line) is False

    @pytest.mark.unit
    def test_non_manifest_blob_error_does_not_suppress_transient_rerun(self) -> None:
        """A transient registry pull (context deadline exceeded) must still trigger
        a rerun when the only nearby daemon error is a non-manifest (blob) permanent
        error for the same repository.

        Scenario: ``docker pull ghcr.io/org/app:good`` times out, but an unrelated
        permanent blob error ``Error response from daemon: Get
        "https://ghcr.io/v2/org/app/blobs/sha256:bad": not found`` appears in the
        same window (e.g. from a concurrent docker operation for a different tag).
        Before the fix, ``_image_ref_matches_daemon_url`` returned True for the
        blob URL solely on the ``/v2/org/app/`` prefix, the permanence probe
        misclassified the summary as permanent, and AWF reported a CI failure
        instead of scheduling a rerun.

        Regression for PRRT_kwDOSJAM6s6Huzsy."""
        from awf.runtime._docker_pull_detection import _log_shows_docker_registry_timeout

        log = "\n".join(
            [
                "/usr/bin/docker pull ghcr.io/org/app:good",
                'error response from daemon: get "https://ghcr.io/v2/org/app/blobs/sha256:bad": not found',
                "docker pull failed with exit code 1",
                "context deadline exceeded",
            ]
        )
        assert _log_shows_docker_registry_timeout(log) is True

    @pytest.mark.unit
    def test_org_image_token_auth_host_in_url_path_does_not_match(self) -> None:
        """An ``auth.docker.io`` token denial whose host is a *different* registry
        must NOT match an unqualified Docker Hub org image, even when
        ``auth.docker.io`` appears only inside the URL path.

        ``docker pull org/app:latest`` is an implicit Docker Hub pull.  A bare
        substring check ``"auth.docker.io" in line`` is satisfied by
        ``https://evil.example/auth.docker.io/token?...`` where ``auth.docker.io``
        is a path segment, not the token-service host — a CodeQL
        ``py/incomplete-url-substring-sanitization`` bypass.  The URL-host
        boundary check (``"//auth.docker.io/"``) rejects it.

        Regression for CodeQL alert #7."""
        bypass_line = (
            'get "https://evil.example/auth.docker.io/token'
            '?scope=repository%3aorg%2fapp%3apull": denied'
        )
        assert _image_ref_matches_daemon_url("org/app:latest", bypass_line) is False

        # Sanity: the real Docker Hub token service still matches.
        real_line = 'get "https://auth.docker.io/token?scope=repository%3aorg%2fapp%3apull": denied'
        assert _image_ref_matches_daemon_url("org/app:latest", real_line) is True

    @pytest.mark.unit
    def test_docker_hub_alias_token_auth_host_in_url_path_does_not_match(self) -> None:
        """A Docker Hub alias ref must NOT match an ``auth.docker.io`` token denial
        whose host is a different registry and that only embeds ``auth.docker.io``
        in the URL path.

        ``docker pull docker.io/library/postgres:16`` resolves through the
        Docker Hub token service (``auth.docker.io``).  A path-only occurrence of
        ``auth.docker.io`` (e.g. ``https://evil.example/auth.docker.io/token``)
        must not satisfy the Docker Hub host guard — only the URL-host boundary
        form ``//auth.docker.io/`` (or ``//docker.io/``) counts.

        Regression for CodeQL alert #8."""
        bypass_line = (
            'get "https://evil.example/auth.docker.io/token'
            '?scope=repository%3alibrary%2fpostgres%3apull": denied'
        )
        assert _image_ref_matches_daemon_url("docker.io/library/postgres:16", bypass_line) is False

        # Sanity: the real Docker Hub token service still matches.
        real_line = (
            'get "https://auth.docker.io/token'
            '?scope=repository%3alibrary%2fpostgres%3apull": denied'
        )
        assert _image_ref_matches_daemon_url("docker.io/library/postgres:16", real_line) is True

    @pytest.mark.unit
    def test_library_image_token_auth_host_in_url_path_does_not_match(self) -> None:
        """An unqualified Docker Hub library image must NOT match an
        ``auth.docker.io`` token denial whose host is a different registry and
        that only embeds ``auth.docker.io`` in the URL path.

        ``docker pull postgres:16`` is an implicit Docker Hub library pull.  A
        path-only ``auth.docker.io`` (``https://evil.example/auth.docker.io/token``)
        must not satisfy the Docker Hub host guard via a bare substring — only
        the boundary form ``//auth.docker.io/`` counts.

        Regression for CodeQL alert #9."""
        bypass_line = (
            'get "https://evil.example/auth.docker.io/token'
            '?scope=repository%3alibrary%2fpostgres%3apull": denied'
        )
        assert _image_ref_matches_daemon_url("postgres:16", bypass_line) is False

        # Sanity: the real Docker Hub token service still matches.
        real_line = (
            'get "https://auth.docker.io/token'
            '?scope=repository%3alibrary%2fpostgres%3apull": denied'
        )
        assert _image_ref_matches_daemon_url("postgres:16", real_line) is True


class TestStripImageTag:
    """Unit tests for ``_strip_image_tag``."""

    @pytest.mark.unit
    def test_tagged_ref_strips_tag(self) -> None:
        assert _strip_image_tag("ghcr.io/org/app:latest") == "ghcr.io/org/app"

    @pytest.mark.unit
    def test_tagged_ref_with_port_strips_tag(self) -> None:
        assert (
            _strip_image_tag("registry.example.com:5000/org/app:v1")
            == "registry.example.com:5000/org/app"
        )

    @pytest.mark.unit
    def test_tagless_ref_unchanged(self) -> None:
        assert _strip_image_tag("ghcr.io/org/app") == "ghcr.io/org/app"

    @pytest.mark.unit
    def test_unqualified_tagless_ref_unchanged(self) -> None:
        assert _strip_image_tag("postgres") == "postgres"

    @pytest.mark.unit
    def test_unqualified_tagged_ref_strips_tag(self) -> None:
        assert _strip_image_tag("postgres:16") == "postgres"

    @pytest.mark.unit
    def test_digest_pinned_ref_strips_to_repo(self) -> None:
        # Regression for PRRT_kwDOSJAM6s6HuBR5: rfind(":") on a digest ref
        # hits the colon inside "sha256:", leaving "@sha256" in the result.
        assert _strip_image_tag("ghcr.io/org/private@sha256:abc123def456") == "ghcr.io/org/private"

    @pytest.mark.unit
    def test_digest_pinned_ref_with_port_strips_to_repo(self) -> None:
        assert (
            _strip_image_tag("registry.example.com:5000/org/app@sha256:deadbeef")
            == "registry.example.com:5000/org/app"
        )


class TestTaglessDaemonDenialMatchesTaggedPull:
    """Regression tests for tagless daemon denial matching (PRRT_kwDOSJAM6s6Ht1mT).

    Docker access-denied errors commonly omit the tag:
    ``pull access denied for ghcr.io/org/private, repository does not exist
    or may require 'docker login': denied``.  When a ``docker pull
    ghcr.io/org/private:latest`` echo is present, the daemon denial must be
    attributed to that pull so the permanence probe returns True and the log
    is classified as a non-retryable CI failure rather than a transient rerun.
    """

    @pytest.mark.unit
    def test_tagless_daemon_denial_backward_probe_reports_ci_failure(self) -> None:
        """A tagless ``pull access denied for <repo>`` daemon error preceding the
        ``docker pull failed`` summary must suppress the transient-timeout rerun.

        Log order: pull echo → daemon denial (tagless) → docker pull failed →
        context deadline exceeded.  Without the tagless-match fix the daemon
        line carries ``ghcr.io/org/private`` (no ``:latest`` token), so the
        exact-token comparison fails and the log is mis-classified as transient.

        Regression for PRRT_kwDOSJAM6s6Ht1mT."""
        failure = CheckFailure(
            name="python-coverage-shards (1)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull ghcr.io/org/private:latest\n"
                "Error response from daemon: pull access denied for ghcr.io/org/private,"
                " repository does not exist or may require 'docker login': denied\n"
                "docker pull failed with exit code 1\n"
                "context deadline exceeded"
            ),
            run_id="99000001001",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_tagless_daemon_denial_forward_probe_reports_ci_failure(self) -> None:
        """A tagless ``pull access denied for <repo>`` daemon error *following* the
        ``docker pull failed`` summary must also suppress the transient-timeout rerun.

        Log order: pull echo → docker pull failed → daemon denial (tagless) →
        context deadline exceeded.  The forward daemon probe must match the
        tagless ref the same way the backward probe does.

        Regression for PRRT_kwDOSJAM6s6Ht1mT."""
        failure = CheckFailure(
            name="python-coverage-shards (1)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull ghcr.io/org/private:latest\n"
                "docker pull failed with exit code 1\n"
                "Error response from daemon: pull access denied for ghcr.io/org/private,"
                " repository does not exist or may require 'docker login': denied\n"
                "context deadline exceeded"
            ),
            run_id="99000001002",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_digest_pinned_tagless_daemon_denial_backward_probe_reports_ci_failure(
        self,
    ) -> None:
        """A tagless daemon denial after a digest-pinned pull must be attributed correctly.

        Log order: digest pull echo → tagless daemon denial → docker pull failed →
        context deadline exceeded.  Without the @-digest strip in ``_strip_image_tag``
        the comparison produces ``ghcr.io/org/private@sha256`` vs the daemon token
        ``ghcr.io/org/private``, so the permanent denial is missed and the log is
        mis-classified as transient.

        Regression for PRRT_kwDOSJAM6s6HuBR5."""
        failure = CheckFailure(
            name="python-coverage-shards (1)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull ghcr.io/org/private@sha256:abc123def456\n"
                "Error response from daemon: pull access denied for ghcr.io/org/private,"
                " repository does not exist or may require 'docker login': denied\n"
                "docker pull failed with exit code 1\n"
                "context deadline exceeded"
            ),
            run_id="99000001003",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_digest_pinned_tagless_daemon_denial_forward_probe_reports_ci_failure(
        self,
    ) -> None:
        """A tagless daemon denial *following* a digest-pinned pull summary must also match.

        Log order: digest pull echo → docker pull failed → tagless daemon denial →
        context deadline exceeded.  The forward daemon probe must apply the same
        @-digest strip so the permanent denial suppresses the transient-timeout rerun.

        Regression for PRRT_kwDOSJAM6s6HuBR5."""
        failure = CheckFailure(
            name="python-coverage-shards (1)",
            conclusion="FAILURE",
            log_excerpt=(
                "/usr/bin/docker pull ghcr.io/org/private@sha256:abc123def456\n"
                "docker pull failed with exit code 1\n"
                "Error response from daemon: pull access denied for ghcr.io/org/private,"
                " repository does not exist or may require 'docker login': denied\n"
                "context deadline exceeded"
            ),
            run_id="99000001004",
        )

        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )

        assert isinstance(action, ReportCiFailure), action
        assert action.failures == (failure,)


class TestImageRefMatchesDaemonUrlEdgeCases:
    """Coverage tests for edge-case branches in ``_image_ref_matches_daemon_url``."""

    @pytest.mark.unit
    def test_ref_with_both_digest_and_tag_uses_digest_as_manifest_ref(self) -> None:
        """When an image ref carries both a digest and a tag (e.g.
        ``ghcr.io/org/app:v1@sha256:abc``), the digest is extracted first (setting
        ``manifest_ref``); the tag-stripping ``if manifest_ref is None`` branch is
        skipped but ``ref_no_tag`` is still set from the tag colon (branch 413->415).
        The manifest URL must match using the digest.
        """
        line = 'Head "https://ghcr.io/v2/org/app/manifests/sha256:abc123": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:v1@sha256:abc123", line) is True

    @pytest.mark.unit
    def test_empty_repo_path_returns_false(self) -> None:
        """A malformed ref that resolves to an empty repo path must return False
        (line 426) rather than building a bare ``/v2//manifests/`` fragment.
        """
        assert (
            _image_ref_matches_daemon_url("registry.example.com/", "any daemon error line") is False
        )

    @pytest.mark.unit
    def test_repo_prefix_without_manifests_does_not_match(self) -> None:
        """A daemon URL with the ``/v2/<repo>/`` prefix but without a
        ``/manifests/<ref>`` path must NOT be attributed to the in-flight pull.

        Non-manifest paths (e.g. ``/tags/list``, ``/blobs/<digest>``) are
        content- or operation-addressed and carry no tag, so the same repository
        prefix appears for operations on *any* tag.  A permanent error on such a
        path for a different image/tag must not suppress a legitimate
        transient-timeout rerun.

        Regression for PRRT_kwDOSJAM6s6Huzsy."""
        line = 'Head "https://ghcr.io/v2/org/app/tags/list": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", line) is False

    @pytest.mark.unit
    def test_library_repo_prefix_without_manifests_does_not_match_docker_hub(self) -> None:
        """A Docker Hub daemon URL with ``/v2/library/<name>/`` but no
        ``/manifests/<ref>`` suffix must NOT match an unqualified Docker Hub image
        ref.

        Same reasoning as the non-library branch: non-manifest paths cannot be
        attributed by tag/digest, so returning False is the safe choice to avoid
        suppressing a transient rerun.

        Regression for PRRT_kwDOSJAM6s6Huzsy."""
        line = 'Head "https://registry-1.docker.io/v2/library/postgres/tags/list": denied'
        assert _image_ref_matches_daemon_url("postgres:16", line) is False

    @pytest.mark.unit
    def test_unqualified_docker_hub_ref_token_scope_matches_hub_host(self) -> None:
        """An unqualified Docker Hub user/org image ref must match a token-service
        denial whose URL carries a Docker Hub registry host (line 557).
        """
        line = (
            'get "https://registry-1.docker.io/token?scope=repository%3aorg%2fapp%3apull": denied'
        )
        assert _image_ref_matches_daemon_url("org/app:latest", line) is True

    @pytest.mark.unit
    def test_docker_hub_alias_host_ref_token_scope_matches_auth_host(self) -> None:
        """A ``docker.io/``-prefixed Docker Hub image ref must match an
        ``auth.docker.io`` token-service denial with the encoded scope (line 563).
        """
        line = 'get "https://auth.docker.io/token?scope=repository%3aorg%2fapp%3apull": denied'
        assert _image_ref_matches_daemon_url("docker.io/org/app:bad", line) is True

    @pytest.mark.unit
    def test_token_scope_push_only_does_not_match_pull(self) -> None:
        """A token-service error whose scope action is ``push`` (not ``pull``) must
        NOT be attributed to an in-flight pull for the same repository.

        When a failed-step log contains a real pull timeout summary followed by an
        unrelated docker-push auth denial for the same repo
        (``scope=repository:org/app:push``), the permanence probe must not mark
        the pull summary as permanent — doing so would suppress the legitimate
        transient-timeout rerun and cause AWF to report a CI failure instead.

        Regression for PRRT_kwDOSJAM6s6Hu-r8."""
        encoded_push_line = (
            'get "https://ghcr.io/token?scope=repository%3aorg%2fapp%3apush": denied'
        )
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", encoded_push_line) is False

        unencoded_push_line = 'get "https://ghcr.io/token?scope=repository:org/app:push": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", unencoded_push_line) is False

    @pytest.mark.unit
    def test_token_scope_pull_push_combined_matches_pull(self) -> None:
        """A token-service error whose scope action is ``pull,push`` must still be
        attributed to the in-flight pull, since the scope includes the pull action.

        ``pull,push`` is the combined-access form Docker uses when an image requires
        both read and write permissions; the pull is still in flight, so a permanent
        denial must be treated as a pull failure.

        Regression for PRRT_kwDOSJAM6s6Hu-r8."""
        line = 'get "https://ghcr.io/token?scope=repository%3aorg%2fapp%3apull%2cpush": denied'
        assert _image_ref_matches_daemon_url("ghcr.io/org/app:latest", line) is True


class TestForwardDetailRefMatchesPull:
    """Coverage tests for ``_forward_detail_ref_matches_pull``."""

    @pytest.mark.unit
    def test_detail_line_without_quoted_ref_returns_false(self) -> None:
        """When the detail line does not contain a quoted image ref matching
        ``failed to pull image "<ref>"``, the function must return False immediately
        (line 347).
        """
        result = _forward_detail_ref_matches_pull(
            "failed to pull image: some unquoted error",
            back_start=0,
            summary_index=1,
            lines=["some prior line", "docker pull failed"],
        )
        assert result is False


class TestEvidenceLinePullImageExtractionEdgeCases:
    """Coverage tests for pull-image extraction edge cases in
    ``_evidence_line_is_permanent_pull_failure`` (lines 684->694, 686-687,
    706->735, 724-725).
    """

    @pytest.mark.unit
    def test_pull_echo_with_only_flags_leaves_preceding_image_none(self) -> None:
        """When the pull echo preceding the summary contains only option flags and
        no image argument, ``non_flags`` is empty (branch 684->694) and
        ``preceding_pull_image`` stays None.  The function must not raise.
        """
        lines = [
            "docker pull --quiet",  # pull echo: no image arg
            "docker pull failed",  # summary (index=1, start=0)
        ]
        assert _evidence_line_is_permanent_pull_failure(1, lines) is False

    @pytest.mark.unit
    def test_pull_echo_no_exact_pull_token_raises_stopiteration_gracefully(self) -> None:
        """When the pull-echo line contains ``docker pull`` as a substring but the
        split token is not the exact word ``pull`` (e.g. ``pull.``), ``next(...)``
        raises StopIteration; the except clause must swallow it gracefully
        (lines 686-687).
        """
        lines = [
            "docker pull.",  # "docker pull" substring; "pull." is not "pull"
            "docker pull failed",  # summary (index=1, start=0)
        ]
        assert _evidence_line_is_permanent_pull_failure(1, lines) is False

    @pytest.mark.unit
    def test_stale_pull_echo_with_only_flags_leaves_stale_guard_none(self) -> None:
        """When a stale pull echo (before the evidence window) contains only option
        flags, ``stale_non_flags`` is empty (branch 706->735) and
        ``stale_guard_image`` stays None.  The function must not raise.

        Stale echo is at index 0; summary at index 3 so
        ``start = max(0, 3-2) = 1`` and ``back_start = 0 < start``.
        """
        lines = [
            "docker pull --quiet",  # stale echo (index 0 < start=1)
            "some other log line",
            "some other log line 2",
            "docker pull failed",  # summary (index=3)
        ]
        assert _evidence_line_is_permanent_pull_failure(3, lines) is False

    @pytest.mark.unit
    def test_stale_pull_echo_no_exact_pull_token_raises_stopiteration_gracefully(self) -> None:
        """When the stale pull-echo line contains ``docker pull`` as a substring but
        no exact ``pull`` token, StopIteration is raised in stale-guard extraction
        and the except clause handles it gracefully (lines 724-725).

        Stale echo is at index 0; summary at index 3 so ``back_start = 0 < start = 1``.
        """
        lines = [
            "docker pull.",  # stale: "docker pull" substring; "pull." != "pull"
            "some other log line",
            "some other log line 2",
            "docker pull failed",  # summary (index=3)
        ]
        assert _evidence_line_is_permanent_pull_failure(3, lines) is False

    @pytest.mark.unit
    def test_pull_echo_with_shell_redirect_does_not_corrupt_preceding_pull_image(
        self,
    ) -> None:
        """When the pull echo preceding the summary has a shell redirection suffix
        (e.g. ``docker pull ghcr.io/org/app:bad 2>&1``), the ``2>&1`` token must
        not be stored as ``preceding_pull_image``.  A backward daemon denial for the
        correct image must still be matched so the summary is classified as a
        permanent (non-retryable) failure.

        Regression for PRRT_kwDOSJAM6s6HuqsM."""
        lines = [
            "docker pull ghcr.io/org/app:bad 2>&1",  # pull echo with redirect
            # Lines are lowercased by _log_shows_docker_registry_timeout before
            # being passed to this function.
            "error response from daemon: pull access denied for ghcr.io/org/app,"
            " repository does not exist or may require 'docker login': denied",
            "docker pull failed with exit code 1",  # summary (index=2)
        ]
        assert _evidence_line_is_permanent_pull_failure(2, lines) is True

    @pytest.mark.unit
    def test_stale_pull_echo_with_shell_redirect_does_not_corrupt_stale_guard_image(
        self,
    ) -> None:
        """When a stale pull echo (before the evidence window) has a shell
        redirection suffix, the ``2>&1`` token must not be stored as
        ``stale_guard_image``.  A forward ``failed to pull image`` detail for the
        correct image must still be matched so the summary is classified as permanent.

        Stale echo at index 0; summary at index 3 (start = max(0, 3-2) = 1, so
        back_start = 0 < start = 1).  Regression for PRRT_kwDOSJAM6s6HuqsM."""
        lines = [
            "docker pull ghcr.io/org/app:bad 2>&1",  # stale echo with redirect
            "some other log line",
            "some other log line 2",
            "docker pull failed with exit code 1",  # summary (index=3)
            'failed to pull image "ghcr.io/org/app:bad" pull access denied',  # forward detail
        ]
        assert _evidence_line_is_permanent_pull_failure(3, lines) is True
