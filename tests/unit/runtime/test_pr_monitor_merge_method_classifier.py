"""Regression tests for PR monitor merge-method classification helpers."""

from __future__ import annotations

import pytest

from awf.common.audit import redact_audit_text
from awf.common.github_client import GitHubClientError
from awf.runtime.pr_monitor_runner.merge_loop import (
    _effective_merge_methods,
    _merge_error_supports_method_alternative,
    _merge_method_rejection_method,
    _MergeAttemptOutcome,
    _MergeAttemptResult,
)


@pytest.mark.unit
def test_effective_merge_methods_intersect_repo_and_branch_constraints() -> None:
    """Effective merge methods prefer squash after intersecting repo and branch policy."""
    assert _effective_merge_methods(
        repo_methods=("merge", "squash", "rebase"),
        branch_methods=("merge",),
    ) == ("merge",)
    assert _effective_merge_methods(
        repo_methods=("merge", "squash"),
        branch_methods=None,
    ) == ("squash", "merge")
    assert (
        _effective_merge_methods(
            repo_methods=("squash",),
            branch_methods=("merge",),
        )
        == ()
    )


@pytest.mark.unit
def test_effective_merge_methods_resolves_fast_forward_only_repo() -> None:
    """A fast-forward-only Bitbucket repo must resolve to ``fast_forward`` (#448).

    Before #448 ``fast_forward`` was absent from ``_MERGE_METHOD_PREFERENCE`` so the
    intersection silently dropped it to an empty tuple, wedging every merge on a
    fast-forward-only repo with a spurious MERGE_METHOD_MISMATCH.
    """
    assert _effective_merge_methods(
        repo_methods=("fast_forward",),
        branch_methods=None,
    ) == ("fast_forward",)


@pytest.mark.unit
def test_effective_merge_methods_prefers_squash_over_fast_forward() -> None:
    """A multi-strategy repo still prefers squash; fast_forward is the last resort."""
    assert _effective_merge_methods(
        repo_methods=("merge", "squash", "fast_forward"),
        branch_methods=None,
    ) == ("squash", "merge", "fast_forward")


@pytest.mark.unit
def test_effective_merge_methods_github_order_unchanged_by_fast_forward() -> None:
    """Adding fast_forward as the tail entry must not reorder GitHub precedence."""
    assert _effective_merge_methods(
        repo_methods=("rebase", "squash", "merge"),
        branch_methods=None,
    ) == ("squash", "merge", "rebase")


@pytest.mark.unit
def test_merge_method_rejection_classifier_is_specific() -> None:
    """Merge-method rejection classification only handles method-specific failures."""
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Squash merges are not allowed on this repository.",
            )
        )
        == "squash"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge commits are not allowed on this repository.",
            )
        )
        == "merge"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Rebase merges are not allowed on this repository.",
            )
        )
        == "rebase"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge method squash merging is not allowed.",
            )
        )
        == "squash"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge method merge commit is not allowed.",
            )
        )
        == "merge"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge method rebase is not allowed.",
            )
        )
        == "rebase"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="HTTP 502 Bad Gateway",
            )
        )
        is None
    )
    assert (
        _merge_error_supports_method_alternative(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            )
        )
        is False
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge squash merges are not allowed",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            )
        )
        is None
    )


@pytest.mark.unit
def test_redaction_preserves_merge_method_policy_phrases() -> None:
    """Classifier policy phrases must survive GitHubClientError stderr redaction."""
    phrases = (
        "Squash merges are not allowed",
        "Merge commits are not allowed",
        "Rebase merges are not allowed",
        "Merge method squash merging is not allowed",
        "Merge method merge commit is not allowed",
        "Merge method rebase is not allowed",
    )

    for phrase in phrases:
        assert phrase.lower() in redact_audit_text(f"GraphQL: {phrase}.").lower()


@pytest.mark.unit
def test_method_blocker_attempt_result_requires_notification_reason() -> None:
    """Method blockers must carry the state value that suppresses repeat merges."""
    with pytest.raises(ValueError, match="requires a notification reason"):
        _MergeAttemptResult(_MergeAttemptOutcome.METHOD_BLOCKER)

    with pytest.raises(ValueError, match="requires a notification reason"):
        _MergeAttemptResult(_MergeAttemptOutcome.METHOD_BLOCKER, notification_reason="")

    blocker = _MergeAttemptResult(
        _MergeAttemptOutcome.METHOD_BLOCKER,
        notification_reason="MERGE_METHOD_MISMATCH: no allowed method succeeded",
    )

    assert (
        blocker.method_blocker_notification_reason
        == "MERGE_METHOD_MISMATCH: no allowed method succeeded"
    )
    assert _MergeAttemptResult(_MergeAttemptOutcome.SUCCESS).notification_reason is None
    assert _MergeAttemptResult(_MergeAttemptOutcome.RETRY_NEXT_METHOD).notification_reason is None
    assert _MergeAttemptResult(_MergeAttemptOutcome.BLOCKER).notification_reason is None
