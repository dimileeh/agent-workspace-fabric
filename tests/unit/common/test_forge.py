"""Tests for the provider-neutral forge seam (issue #345 Phase 1).

Phase 1 ships the abstraction + detection only: GitHub is the sole concrete
implementation, and a BitBucket forge is *detected* then *fails fast* with a
reason-coded error. These tests lock that contract and the structural
conformance of ``GitHubClient`` to the ``ForgeClient`` Protocol.
"""

from __future__ import annotations

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.forge import (
    _SUPPORTED_FORGES,
    FORGE_NOT_SUPPORTED_REASON_CODE,
    ForgeClient,
    ForgeNotSupportedError,
    concrete_forge,
    concrete_forge_for_repo,
    detect_forge_from_url,
    ensure_forge_supported,
    make_forge_client,
)
from awf.common.github_client import GitHubClient


@pytest.mark.unit
def test_make_forge_client_github_returns_github_client() -> None:
    runner = FakeCommandRunner()
    client = make_forge_client("github", runner)
    assert isinstance(client, GitHubClient)


@pytest.mark.unit
def test_make_forge_client_bitbucket_returns_bitbucket_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Issue #345 Part 2 flips the gate: bitbucket is now a supported forge and the
    # factory builds a BitBucketClient from the env auth contract (was: raised
    # ForgeNotSupportedError in Part 1).
    from awf.common.bitbucket_client import BitBucketClient

    monkeypatch.setenv("BITBUCKET_AUTH_MODE", "bearer")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "tok")
    runner = FakeCommandRunner()
    client = make_forge_client("bitbucket", runner)
    assert isinstance(client, BitBucketClient)
    assert "bitbucket" in _SUPPORTED_FORGES


@pytest.mark.unit
def test_make_forge_client_unknown_value_fails_closed() -> None:
    """A non-literal forge value must fail closed, not silently route to GitHub."""
    runner = FakeCommandRunner()
    with pytest.raises(ForgeNotSupportedError) as excinfo:
        make_forge_client("gitlab", runner)  # type: ignore[arg-type]
    assert excinfo.value.reason_code == FORGE_NOT_SUPPORTED_REASON_CODE
    assert "gitlab" not in _SUPPORTED_FORGES


@pytest.mark.unit
def test_ensure_forge_supported_github_is_noop() -> None:
    # Must not raise.
    ensure_forge_supported("github")


@pytest.mark.unit
def test_ensure_forge_supported_bitbucket_is_noop() -> None:
    # Part 2 flip: bitbucket is supported, so the gate no longer raises.
    ensure_forge_supported("bitbucket")


@pytest.mark.unit
def test_ensure_forge_supported_unknown_forge_raises() -> None:
    with pytest.raises(ForgeNotSupportedError) as excinfo:
        ensure_forge_supported("gitlab")  # type: ignore[arg-type]
    assert excinfo.value.reason_code == FORGE_NOT_SUPPORTED_REASON_CODE


@pytest.mark.unit
def test_github_client_satisfies_forge_client_protocol() -> None:
    runner = FakeCommandRunner()
    client = GitHubClient(runner)
    assert isinstance(client, ForgeClient)


@pytest.mark.unit
def test_forge_client_protocol_typing_accepts_github_client() -> None:
    """A ``GitHubClient`` is accepted where a ``ForgeClient`` is expected."""

    def _consume(forge: ForgeClient) -> ForgeClient:
        return forge

    runner = FakeCommandRunner()
    client = GitHubClient(runner)
    assert _consume(client) is client


@pytest.mark.unit
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/o/r.git", "github"),
        ("git@github.com:o/r.git", "github"),
        ("ssh://git@github.com/o/r.git", "github"),
        ("o/r", "github"),
        ("https://bitbucket.org/ws/repo.git", "bitbucket"),
        ("https://bitbucket.org/ws/repo", "bitbucket"),
        ("git@bitbucket.org:ws/repo.git", "bitbucket"),
        ("ssh://git@bitbucket.org/ws/repo.git", "bitbucket"),
    ],
)
def test_detect_forge_from_url_recognized_hosts(url: str, expected: str) -> None:
    assert detect_forge_from_url(url) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "git@gitlab.com:org/repo.git",
        "https://gitlab.com/org/repo.git",
        "not a url",
        "",
    ],
)
def test_detect_forge_from_url_unknown_or_malformed_returns_none(url: str) -> None:
    assert detect_forge_from_url(url) is None


@pytest.mark.unit
@pytest.mark.parametrize("forge", [None, "", "auto"])
def test_concrete_forge_maps_non_concrete_to_github(forge: object) -> None:
    assert concrete_forge(forge) == "github"


@pytest.mark.unit
@pytest.mark.parametrize("forge", ["github", "bitbucket", "gitlab"])
def test_concrete_forge_passes_concrete_values_through(forge: object) -> None:
    assert concrete_forge(forge) == forge


@pytest.mark.unit
@pytest.mark.parametrize("forge", [None, "", "auto"])
def test_concrete_forge_for_repo_detects_bitbucket_when_forge_absent(forge: object) -> None:
    """A missing/legacy snapshot forge must be detected from the repo URL."""
    assert concrete_forge_for_repo(forge, "git@bitbucket.org:ws/repo.git") == "bitbucket"


@pytest.mark.unit
@pytest.mark.parametrize("forge", [None, "", "auto"])
def test_concrete_forge_for_repo_detects_github_when_forge_absent(forge: object) -> None:
    assert concrete_forge_for_repo(forge, "https://github.com/o/r.git") == "github"


@pytest.mark.unit
@pytest.mark.parametrize("repo_url", [None, "", "not a url", "git@gitlab.com:o/r.git"])
def test_concrete_forge_for_repo_defaults_to_github_when_url_undetected(
    repo_url: str | None,
) -> None:
    """Absent forge + undetectable URL falls back to github, like the resolver."""
    assert concrete_forge_for_repo(None, repo_url) == "github"


@pytest.mark.unit
def test_concrete_forge_for_repo_trusts_concrete_snapshot_over_url() -> None:
    """An explicitly persisted concrete forge wins over URL detection (resolver precedence)."""
    assert concrete_forge_for_repo("github", "git@bitbucket.org:ws/repo.git") == "github"
    assert concrete_forge_for_repo("bitbucket", "https://github.com/o/r.git") == "bitbucket"


@pytest.mark.unit
def test_forge_not_supported_error_is_specific_exception() -> None:
    """The error is its own type (catch specifically, never bare Exception)."""
    assert issubclass(ForgeNotSupportedError, Exception)
    err = ForgeNotSupportedError(message="boom", reason_code="FORGE_NOT_SUPPORTED")
    assert err.message == "boom"
    assert err.reason_code == "FORGE_NOT_SUPPORTED"
