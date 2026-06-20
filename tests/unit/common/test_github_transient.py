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
def test_deterministic_github_auth_error_wins_over_resubmit_wording() -> None:
    assert not is_transient_github_error_text(
        operation="gh api graphql",
        stderr="Bad credentials. Please try resubmitting your request.",
    )
