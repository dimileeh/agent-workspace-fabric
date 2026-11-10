"""Operator-hint prompt: reset, don't revert, a preserved protected commit (#1200)."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_prompts import operator_hint_prompt

pytestmark = pytest.mark.unit

_PRESERVED = "3" * 40
_PUBLISHED = "8" * 40


def _prompt(**kwargs: str | None) -> str:
    return operator_hint_prompt(
        pr_number=7,
        repo_slug="example/service",
        reason="drop the workflow change",
        directive="discard the protected ci.yml commit and resume",
        operation_id="op_1",
        **kwargs,
    )


def test_preserved_commit_prompt_says_reset_to_published_head_not_revert() -> None:
    prompt = _prompt(preserved_head_sha=_PRESERVED, published_head_sha=_PUBLISHED)

    assert f"AWF preserved the unpushed commit `{_PRESERVED}`" in prompt
    assert f"move the branch back to the published PR head `{_PUBLISHED}`" in prompt
    assert "`git reset --hard <published head>`" in prompt
    assert "Do not add a revert commit on top" in prompt
    assert "a revert keeps the preserved commit in the push range" in prompt
    assert f"`git merge-base --is-ancestor {_PRESERVED} HEAD` exits non-zero" in prompt
    # AWF guidance, not part of the untrusted operator evidence block.
    assert prompt.index("Protected-scope block:") > prompt.index("UNTRUSTED EXTERNAL EVIDENCE")


def test_preserved_commit_prompt_without_published_sha_names_the_pr_head() -> None:
    prompt = _prompt(preserved_head_sha=_PRESERVED)

    assert "move the branch back to the published PR head (for example" in prompt
    assert "Do not add a revert commit on top" in prompt


def test_prompt_without_preserved_commit_is_unchanged() -> None:
    assert _prompt(published_head_sha=_PUBLISHED) == _prompt()
    assert "Protected-scope block:" not in _prompt()
