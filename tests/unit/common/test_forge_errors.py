"""Tests for the shared ``ForgeClientError`` base and normalized accessors (#444).

``GitHubClientError`` and ``BitbucketClientError`` historically shared no base, so
monitor catch sites that only listed ``except GitHubClientError`` let Bitbucket
faults escape uncaught. The shared base lets a single ``except ForgeClientError``
catch both; the normalized accessors expose every field the catch sites read over
both subclasses without renaming the subclass-specific fields.
"""

from __future__ import annotations

import pytest

from awf.common.bitbucket_client import (
    BITBUCKET_AUTH_FAILED,
    BITBUCKET_RATE_LIMITED,
    BitbucketClientError,
)
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import GITHUB_API_ERROR, GitHubClientError


@pytest.mark.unit
def test_base_normalized_accessor_defaults() -> None:
    """The base provides safe defaults for the normalized accessors.

    Concrete subclasses override these, but the base contract must be self-consistent
    so a catch site typed on ``ForgeClientError`` always has values to read.
    """
    exc = ForgeClientError("boom")
    assert exc.redacted_detail() == "boom"
    assert exc.merge_method_stderr() == ""
    assert exc.http_status is None
    # The base must be safe to read on the uniform contract even when a subclass
    # forgets to set them (or the base itself is instantiated): a catch site typed
    # on ``ForgeClientError`` reads ``reason_code`` without an ``AttributeError``.
    assert exc.reason_code == "FORGE_CLIENT_ERROR"
    assert exc.operation == ""


@pytest.mark.unit
def test_github_client_error_is_forge_client_error() -> None:
    """A GitHub fault is a ``ForgeClientError`` so the neutral catch arm sees it."""
    exc = GitHubClientError(operation="gh pr view", returncode=1, stderr="HTTP 502 Bad Gateway")
    assert isinstance(exc, ForgeClientError)


@pytest.mark.unit
def test_bitbucket_client_error_is_forge_client_error() -> None:
    """A Bitbucket fault is a ``ForgeClientError`` so the neutral catch arm sees it."""
    exc = BitbucketClientError(operation="bitbucket merge_pr", status=403, body="forbidden")
    assert isinstance(exc, ForgeClientError)


@pytest.mark.unit
def test_github_normalized_accessors() -> None:
    """GitHub normalizes detail/merge-stderr to its stderr and has no HTTP status."""
    exc = GitHubClientError(
        operation="gh pr merge",
        returncode=1,
        stderr="GraphQL: Squash merges are not allowed on this repository.",
    )
    # GitHub carries no stable reason_code natively; the base defaults it.
    assert exc.reason_code == GITHUB_API_ERROR
    assert exc.redacted_detail() == exc.stderr
    # The merge-method-mismatch parser consumes the stderr-equivalent via the base.
    assert exc.merge_method_stderr() == exc.stderr
    # gh is shelled out — there is no HTTP status.
    assert exc.http_status is None
    # Existing field reads keep working unchanged.
    assert exc.returncode == 1
    assert "squash merges are not allowed" in exc.stderr.lower()


@pytest.mark.unit
def test_bitbucket_normalized_accessors() -> None:
    """Bitbucket normalizes detail to its body, carries an HTTP status, no merge stderr."""
    exc = BitbucketClientError(
        operation="bitbucket merge_pr",
        status=429,
        body="rate limited",
        reason_code=BITBUCKET_RATE_LIMITED,
    )
    assert exc.reason_code == BITBUCKET_RATE_LIMITED
    assert exc.redacted_detail() == exc.body
    # Bitbucket carries no per-method rejection signal for the GH stderr parser.
    assert exc.merge_method_stderr() == ""
    assert exc.http_status == 429
    # Existing field reads keep working unchanged.
    assert exc.status == 429
    assert exc.body == "rate limited"


@pytest.mark.unit
def test_bitbucket_transport_error_has_none_http_status() -> None:
    """A transport-level Bitbucket fault exposes ``http_status`` ``None``."""
    exc = BitbucketClientError(
        operation="bitbucket fetch_pr_status",
        status=None,
        body="connection reset",
        reason_code=BITBUCKET_AUTH_FAILED,
    )
    assert exc.http_status is None
    assert exc.reason_code == BITBUCKET_AUTH_FAILED
