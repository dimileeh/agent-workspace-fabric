"""Forge detection + precedence in profile resolution (issue #345 Phase 1).

Precedence (locked): explicit ``forge:`` > URL-host detection > default github.
The resolved concrete forge is persisted ONCE on ``resolved_profile.forge`` and
read back without re-resolving.
"""

from __future__ import annotations

import pytest

from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import resolve_workspace_profile

_GITHUB_URL = "https://github.com/dimileeh/aira-web.git"
_BITBUCKET_URL = "https://bitbucket.org/dimileeh/aira-web.git"


def _inline(forge: str | None) -> dict[str, object]:
    profile: dict[str, object] = {"name": "p", "docker": {"mode": "none"}}
    if forge is not None:
        profile["forge"] = forge
    return profile


@pytest.mark.unit
def test_profile_schema_defaults_forge_to_auto() -> None:
    profile = WorkspaceProfile.model_validate({"name": "p"})
    assert profile.forge == "auto"


@pytest.mark.unit
def test_auto_github_url_resolves_github() -> None:
    resolution = resolve_workspace_profile(
        worktree_path=None,
        inline_profile=_inline("auto"),
        repo_url=_GITHUB_URL,
    )
    assert resolution.profile.forge == "github"


@pytest.mark.unit
def test_auto_bitbucket_url_resolves_bitbucket() -> None:
    resolution = resolve_workspace_profile(
        worktree_path=None,
        inline_profile=_inline("auto"),
        repo_url=_BITBUCKET_URL,
    )
    assert resolution.profile.forge == "bitbucket"


@pytest.mark.unit
def test_explicit_github_overrides_bitbucket_url() -> None:
    resolution = resolve_workspace_profile(
        worktree_path=None,
        inline_profile=_inline("github"),
        repo_url=_BITBUCKET_URL,
    )
    assert resolution.profile.forge == "github"


@pytest.mark.unit
def test_explicit_bitbucket_overrides_github_url() -> None:
    resolution = resolve_workspace_profile(
        worktree_path=None,
        inline_profile=_inline("bitbucket"),
        repo_url=_GITHUB_URL,
    )
    assert resolution.profile.forge == "bitbucket"


@pytest.mark.unit
def test_auto_without_repo_url_defaults_github() -> None:
    resolution = resolve_workspace_profile(
        worktree_path=None,
        inline_profile=_inline("auto"),
    )
    assert resolution.profile.forge == "github"


@pytest.mark.unit
def test_auto_with_malformed_repo_url_defaults_github() -> None:
    resolution = resolve_workspace_profile(
        worktree_path=None,
        inline_profile=_inline("auto"),
        repo_url="not a url",
    )
    assert resolution.profile.forge == "github"


@pytest.mark.unit
def test_resolved_forge_persists_and_reads_back_concrete() -> None:
    """resolved_profile round-trips a concrete forge; later stages do not re-resolve."""
    resolution = resolve_workspace_profile(
        worktree_path=None,
        inline_profile=_inline("auto"),
        repo_url=_BITBUCKET_URL,
    )
    persisted = resolution.profile.model_dump(mode="json", by_alias=True)
    assert persisted["forge"] == "bitbucket"
    # Reconstruct from the persisted snapshot (no repo_url, no re-resolution).
    reconstructed = WorkspaceProfile.model_validate(persisted)
    assert reconstructed.forge == "bitbucket"


@pytest.mark.unit
def test_default_inline_profile_without_forge_resolves_github() -> None:
    resolution = resolve_workspace_profile(
        worktree_path=None,
        inline_profile=_inline(None),
        repo_url=_GITHUB_URL,
    )
    assert resolution.profile.forge == "github"
