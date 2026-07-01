"""Focused GitManager ref-pattern tests."""

from __future__ import annotations

import pytest

import awf.node.git_manager as git_manager


@pytest.mark.unit
def test_github_pull_head_ref_pattern_matches_expected() -> None:
    """Verify github pull head ref pattern matches expected."""
    pattern = git_manager._GITHUB_PULL_HEAD_REF
    assert pattern.match("refs/pull/278/head")
    assert pattern.match("refs/pull/0/head") is None
