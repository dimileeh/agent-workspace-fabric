"""Merge-method policy resolution, classification, and messaging helpers.

Extracted from ``merge_loop`` so that module stays within the first-party
line limit (``test_first_party_code_files_stay_under_line_limit``). These are
the pure (and one ``self``-taking) helpers that decide which merge method to
use, classify forge rejections, and build operator-facing messages; the merge
action orchestration that calls them lives in ``merge_loop``.
"""

from __future__ import annotations

from typing import Any

from awf.common.github_client import GitHubClientError, RepoRef
from awf.runtime.pr_monitor_runner.helpers import _redact_and_truncate_forge_error

# ``fast_forward`` is intentionally LAST so squash stays the default and the
# GitHub precedence (squash > merge > rebase) is unchanged: it only ever wins on a
# repo/branch policy that allows no other method (e.g. a fast-forward-only Bitbucket
# repo, which previously wedged on a spurious MERGE_METHOD_MISMATCH — issue #448).
# AWF preference wins among allowed strategies; Bitbucket's ``default_merge_strategy``
# is ignored, uniform with GitHub.
_MERGE_METHOD_PREFERENCE = ("squash", "merge", "rebase", "fast_forward")
_KNOWN_MERGE_METHODS = frozenset(_MERGE_METHOD_PREFERENCE)
_MERGE_METHOD_MISMATCH_REASON = "MERGE_METHOD_MISMATCH"


def _effective_merge_methods(
    *,
    repo_methods: tuple[str, ...],
    branch_methods: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Intersect repo and branch merge policies in AWF preference order."""
    repo_allowed = {method for method in repo_methods if method in _KNOWN_MERGE_METHODS}
    branch_allowed = (
        None
        if branch_methods is None
        else {method for method in branch_methods if method in _KNOWN_MERGE_METHODS}
    )
    allowed = repo_allowed if branch_allowed is None else repo_allowed.intersection(branch_allowed)
    return tuple(method for method in _MERGE_METHOD_PREFERENCE if method in allowed)


async def _resolve_effective_merge_methods(
    self: Any,
    *,
    repo: RepoRef,
    base_branch: str,
) -> tuple[str, ...]:
    """Fetch repository and base-branch policy before selecting merge methods.

    ``self`` is the ``PullRequestMonitorRunner`` instance; this follows the
    extract-to-module-function pattern used throughout this module.
    """
    repo_methods = await self._deps.gh.fetch_repo_merge_methods(repo=repo)
    branch_methods = await self._deps.gh.fetch_branch_pull_request_allowed_merge_methods(
        repo=repo,
        branch=base_branch,
    )
    return _effective_merge_methods(repo_methods=repo_methods, branch_methods=branch_methods)


def _merge_method_rejection_method(exc: GitHubClientError) -> str | None:
    """Return the merge method explicitly rejected by GitHub, when known."""
    # GitHubClientError stores redacted stderr. These policy phrases contain no
    # secret-like material, so redaction should preserve the classifier signal.
    text = exc.stderr.lower()
    if (
        "squash merges are not allowed" in text
        or "merge method squash merging is not allowed" in text
    ):
        return "squash"
    if (
        "merge commits are not allowed" in text
        or "merge method merge commit is not allowed" in text
    ):
        return "merge"
    if "rebase merges are not allowed" in text or "merge method rebase is not allowed" in text:
        return "rebase"
    return None


def _merge_error_supports_method_alternative(exc: GitHubClientError) -> bool:
    """Detect GitHub merge failures that can be retried with another method."""
    return _merge_method_rejection_method(exc) is not None


def _merge_method_mismatch_message(
    *,
    base_branch: str,
    attempted_method: str | None,
    effective_methods: tuple[str, ...],
    detail: str | None = None,
) -> str:
    """Build the human-facing merge-method mismatch notification."""
    allowed = ", ".join(effective_methods) if effective_methods else "none"
    attempted = attempted_method or "none"
    suffix = f" GitHub reported: {detail}" if detail else ""
    return (
        "MERGE_METHOD_MISMATCH: AWF could not merge this PR because no merge "
        f"method succeeded for base branch {base_branch!r}. "
        f"attempted={attempted}; effective_allowed={allowed}.{suffix}"
    )[:2000]


def _merge_method_preflight_rejection_reason(exc: GitHubClientError) -> str:
    """Build an operator-facing reason for merge-method policy preflight failures.

    Only called with ``GitHubClientError``; ``BitbucketClientError`` has no
    ``stderr`` attribute and is handled separately by
    ``_bitbucket_merge_rejection_reason``.
    """
    detail = " ".join(_redact_and_truncate_forge_error(exc.stderr).split())[:240]
    if detail:
        return f"GitHub rejected merge-method preflight: {detail}"
    return "GitHub rejected merge-method preflight"


def _merge_completion_marker(
    *,
    merge_sha: str | None,
    head_sha: str,
) -> str:
    """Return the non-empty marker used to record a completed PR merge."""
    normalized_merge_sha = merge_sha.strip() if merge_sha else None
    if normalized_merge_sha:
        return normalized_merge_sha
    return head_sha
