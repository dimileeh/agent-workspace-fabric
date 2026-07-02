"""Regression coverage for shared GitHub transient-failure classification."""

from __future__ import annotations

import pytest

from awf.common.github_transient import is_transient_github_error_text


@pytest.mark.unit
def test_ws_88b71225_connection_error_is_transient() -> None:
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(
            operation="gh api graphql",
            stderr="gh api graphql failed (exit=1): error connecting to api.github.com",
        )
        == GitHubErrorDisposition.TRANSIENT
    )


@pytest.mark.unit
def test_unknown_github_error_defaults_to_unknown() -> None:
    """A gh failure matching no known marker is UNKNOWN, not TRANSIENT. The two
    layers then treat it differently: the transport still retries UNKNOWN in-cycle
    (allow-by-default), but the monitor's terminate-vs-keep-polling decision FAILS on
    it rather than polling forever on a fault it cannot classify."""
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(
            operation="gh api graphql",
            stderr="something completely new from gh",
        )
        == GitHubErrorDisposition.UNKNOWN
    )


@pytest.mark.unit
def test_already_exists_is_not_permanent() -> None:
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(
            operation="gh pr create",
            stderr='HTTP 422: A pull request already exists for "o:feature".',
        )
        != GitHubErrorDisposition.PERMANENT
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "HTTP 403: permission denied (repository already exists in org)",
        "branch protection rule already exists; access denied",
    ],
)
def test_already_exists_does_not_suppress_other_permanent_markers(stderr: str) -> None:
    """The duplicate-PR ``already exists`` exclusion must not blanket-skip the
    PERMANENT set: an ``already exists`` stderr that lacks the ``pull request``
    duplicate-create signal but carries a real permanent marker still fails fast."""
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(operation="gh api graphql", stderr=stderr)
        == GitHubErrorDisposition.PERMANENT
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "not logged in to any GitHub hosts",
        "HTTP 403: Resource not accessible by integration",
        "SSO authorization required",
        "repository is archived",
        "Field 'missingField' doesn't exist on type 'PullRequest'",
    ],
)
def test_permanent_markers_fast_fail(stderr: str) -> None:
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(operation="gh api graphql", stderr=stderr)
        == GitHubErrorDisposition.PERMANENT
    )


@pytest.mark.unit
def test_generic_malformed_http_400_without_resubmit_guidance_is_unknown() -> None:
    """A bare HTTP 400 (not a 5xx / rate-limit / connection marker) is UNKNOWN:
    the transport retries it in-cycle, but it is not a recognized transient."""
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(
            operation="gh pr create",
            stderr="HTTP 400: malformed request",
        )
        == GitHubErrorDisposition.UNKNOWN
    )


@pytest.mark.unit
def test_resubmit_wording_without_github_api_context_is_unknown() -> None:
    """``please try resubmitting`` only maps to TRANSIENT with GitHub API context;
    without it (a local validator here) it is UNKNOWN."""
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(
            operation="local validator",
            stderr="malformed payload; please try resubmitting",
        )
        == GitHubErrorDisposition.UNKNOWN
    )


@pytest.mark.unit
def test_malformed_graphql_resubmit_error_is_transient() -> None:
    assert is_transient_github_error_text(
        operation="gh pr create",
        stderr=(
            "pull request create failed: HTTP 400: We received a malformed request "
            "from your client. Sorry about that. Please try resubmitting your "
            "request and contact us if the problem persists. "
            "(https://api.github.com/graphql)"
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "HTTP 503 from GitHub",
        "gateway timeout while contacting api.github.com",
        "connection reset by peer",
    ],
)
def test_github_server_and_network_markers_are_transient(stderr: str) -> None:
    assert is_transient_github_error_text(
        operation="gh api graphql",
        stderr=stderr,
    )


@pytest.mark.unit
def test_github_bad_credentials_401_is_ambiguous_transient() -> None:
    assert is_transient_github_error_text(
        operation="gh api graphql",
        stderr="gh api graphql failed (exit=1): gh: Bad credentials (HTTP 401)",
    )


@pytest.mark.unit
def test_bare_github_api_bad_credentials_is_not_transient_without_401_evidence() -> None:
    assert not is_transient_github_error_text(
        operation="gh api graphql",
        stderr="Bad credentials",
    )


@pytest.mark.unit
def test_bare_pr_create_bad_credentials_without_auth_context_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="gh pr create",
        stderr="Bad credentials",
    )


@pytest.mark.unit
def test_pr_create_bad_credentials_with_generic_try_again_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="gh pr create",
        stderr="Bad credentials. Please try again.",
    )


@pytest.mark.unit
def test_pr_create_bad_credentials_401_without_api_context_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="gh pr create",
        stderr="gh: Bad credentials (HTTP 401)",
    )


@pytest.mark.unit
def test_bad_credentials_401_without_github_operation_context_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="git push",
        stderr="gh: Bad credentials (HTTP 401)",
    )


@pytest.mark.unit
def test_pr_create_bad_credentials_401_with_graphql_stderr_context_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="gh pr create",
        stderr="https://api.github.com/graphql: Bad credentials (HTTP 401)",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "operation",
    ["gh api graphql", "gh pr create", "gh pr merge", "gh pr comment"],
)
def test_bad_credentials_with_resubmit_guidance_without_auth_form_is_not_transient(
    operation: str,
) -> None:
    assert not is_transient_github_error_text(
        operation=operation,
        stderr="Bad credentials. Please try resubmitting your request.",
    )


@pytest.mark.unit
def test_api_bad_credentials_with_resubmit_guidance_and_401_is_transient() -> None:
    assert is_transient_github_error_text(
        operation="gh api graphql",
        stderr="Bad credentials (HTTP 401). Please try resubmitting your request.",
    )


@pytest.mark.unit
def test_pr_create_bad_credentials_with_resubmit_guidance_and_401_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="gh pr create",
        stderr=(
            "https://api.github.com/graphql: Bad credentials (HTTP 401). "
            "Please try resubmitting your request."
        ),
    )


@pytest.mark.unit
def test_pr_create_requires_authentication_resubmit_guidance_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="gh pr create",
        stderr=("gh: Requires authentication (HTTP 401). Please try resubmitting your request."),
    )


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["gh pr merge", "gh pr comment"])
def test_pr_cli_requires_authentication_resubmit_guidance_is_transient(operation: str) -> None:
    assert is_transient_github_error_text(
        operation=operation,
        stderr=(
            "https://api.github.com/graphql: Requires authentication (HTTP 401). "
            "Please try resubmitting your request."
        ),
    )


@pytest.mark.unit
def test_pr_create_requires_authentication_without_resubmit_guidance_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="gh pr create",
        stderr="gh: Requires authentication (HTTP 401)",
    )


@pytest.mark.unit
def test_pr_create_requires_authentication_with_generic_try_again_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="gh pr create",
        stderr="gh: Requires authentication (HTTP 401). Please try again.",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "not logged in to any GitHub hosts",
        "To get started with GitHub CLI, please run gh auth login",
        "repository not found",
        "could not resolve to a Repository with the name 'org/repo'",
        "could not resolve to a node",
    ],
)
def test_deterministic_github_auth_and_not_found_markers_stay_non_transient(
    stderr: str,
) -> None:
    assert not is_transient_github_error_text(
        operation="gh api graphql",
        stderr=stderr,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "HTTP 401",
        "Requires authentication",
        "gh: Requires authentication (HTTP 401)",
    ],
)
def test_ambiguous_github_401_markers_stay_transient(stderr: str) -> None:
    assert is_transient_github_error_text(
        operation="gh api graphql",
        stderr=stderr,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("operation", "stderr"),
    [
        ("gh pr merge", "gh: Requires authentication (HTTP 401)"),
        ("gh pr comment", "HTTP 401"),
        ("gh pr merge", "gh: Bad credentials (HTTP 401)"),
        ("gh pr comment", "gh: Bad credentials (HTTP 401)"),
    ],
)
def test_ambiguous_github_401_markers_are_transient_for_pr_cli_operations(
    operation: str,
    stderr: str,
) -> None:
    assert is_transient_github_error_text(
        operation=operation,
        stderr=stderr,
    )


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["list_runs_for_sha", "view_run_log"])
def test_failed_ci_log_fetch_ambiguous_401_markers_are_transient(operation: str) -> None:
    assert is_transient_github_error_text(
        operation=operation,
        stderr="gh: Requires authentication (HTTP 401)",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "gh: Requires authentication (HTTP 401)",
        "gh: Bad credentials (HTTP 401)",
    ],
)
def test_deferred_issue_create_ambiguous_401_markers_are_transient(stderr: str) -> None:
    assert is_transient_github_error_text(
        operation="gh issue create",
        stderr=stderr,
    )


@pytest.mark.unit
def test_resubmit_with_auth_evidence_but_no_ambiguous_context_is_permanent() -> None:
    """Resubmit guidance + API context + auth evidence, but the operation lacks
    ambiguous-auth context -> PERMANENT (a create/auth fault, not a retryable blip)."""
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(
            operation="local validator",
            stderr="please try resubmitting https://api.github.com/graphql (HTTP 401)",
        )
        == GitHubErrorDisposition.PERMANENT
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "HTTP 403: API rate limit exceeded for installation (https://api.github.com/graphql)",
        "HTTP 403: You have exceeded a secondary rate limit. Please wait a few minutes.",
        "HTTP 403: You have triggered an abuse detection mechanism. Please retry your request.",
    ],
)
def test_rate_limit_403_is_transient_not_permanent(stderr: str) -> None:
    """GitHub reports primary/secondary rate-limit throttling as HTTP 403 while
    also emitting a rate-limit marker. The generic ``http 403`` permanent marker
    must NOT win over the recoverable rate-limit markers, or the monitor would
    terminate instead of retrying a transient throttle."""
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(operation="gh api graphql", stderr=stderr)
        == GitHubErrorDisposition.TRANSIENT
    )
    assert is_transient_github_error_text(operation="gh api graphql", stderr=stderr)


@pytest.mark.unit
def test_permission_denied_403_stays_permanent_despite_rate_limit_guard() -> None:
    """A genuine permission 403 carries its own specific marker and no rate-limit
    marker, so the rate-limit guard does not weaken it to transient."""
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(
            operation="gh api graphql",
            stderr="HTTP 403: Resource not accessible by integration",
        )
        == GitHubErrorDisposition.PERMANENT
    )


@pytest.mark.unit
def test_ambiguous_auth_marker_without_context_is_permanent() -> None:
    """A 401 auth marker with no ambiguous-auth *context* is a deterministic auth
    fault (PERMANENT), not the #515 transient-401 case."""
    from awf.common.github_transient import (
        GitHubErrorDisposition,
        github_error_disposition,
    )

    assert (
        github_error_disposition(operation="local validator", stderr="HTTP 401 Unauthorized")
        == GitHubErrorDisposition.PERMANENT
    )
