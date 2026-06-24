"""Regression coverage for shared GitHub transient-failure classification."""

from __future__ import annotations

import pytest

from awf.common.github_transient import is_transient_github_error_text


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
def test_generic_malformed_http_400_without_resubmit_guidance_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="gh pr create",
        stderr="HTTP 400: malformed request",
    )


@pytest.mark.unit
def test_resubmit_wording_without_github_api_context_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="local validator",
        stderr="malformed payload; please try resubmitting",
    )


@pytest.mark.unit
def test_github_bad_credentials_401_is_ambiguous_transient() -> None:
    assert is_transient_github_error_text(
        operation="gh api graphql",
        stderr="gh api graphql failed (exit=1): gh: Bad credentials (HTTP 401)",
    )


@pytest.mark.unit
def test_bare_github_api_bad_credentials_is_ambiguous_transient() -> None:
    assert is_transient_github_error_text(
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
def test_pr_create_bad_credentials_401_without_api_context_is_not_transient() -> None:
    assert not is_transient_github_error_text(
        operation="gh pr create",
        stderr="gh: Bad credentials (HTTP 401)",
    )


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["gh api graphql", "gh pr create"])
def test_bad_credentials_with_resubmit_guidance_without_auth_form_is_not_transient(
    operation: str,
) -> None:
    assert not is_transient_github_error_text(
        operation=operation,
        stderr="Bad credentials. Please try resubmitting your request.",
    )


@pytest.mark.unit
def test_bad_credentials_with_resubmit_guidance_and_401_is_transient() -> None:
    assert is_transient_github_error_text(
        operation="gh api graphql",
        stderr="Bad credentials (HTTP 401). Please try resubmitting your request.",
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
