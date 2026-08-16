"""Reasoning-guidance regression coverage for monitor prompts."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
    fix_ci_prompt,
)
from awf.runtime.pr_monitor import CheckFailure, ReviewComment, ReviewThread


class TestReasoningGuidance:
    """The monitor prompts guide disciplined, narrowly scoped decisions."""

    @pytest.mark.unit
    def test_thread_prompt_includes_comment_verdict_reasoning(self) -> None:
        thread = ReviewThread(thread_id="T", path="x", line=1, body_excerpt="x")
        prompt = address_thread_prompt(pr_number=1, repo_slug="a/b", thread=thread)
        assert "Verify the claim against the actual code first" in prompt
        assert "real defect or reviewer breadth-conservatism" in prompt
        assert "do not refactor unrelated code or expand the PR" in prompt

    @pytest.mark.unit
    def test_review_comment_prompt_includes_comment_verdict_reasoning(self) -> None:
        comment = ReviewComment(comment_id="C", body_excerpt="x")
        prompt = address_review_comment_prompt(pr_number=1, repo_slug="a/b", comment=comment)
        assert "Verify the claim against the actual code first" in prompt
        assert "do not refactor unrelated code or expand the PR" in prompt

    @pytest.mark.unit
    def test_fix_ci_prompt_includes_coverage_reasoning(self) -> None:
        failures = (CheckFailure(name="coverage-gate", conclusion="FAILURE", log_excerpt="x"),)
        prompt = fix_ci_prompt(pr_number=1, repo_slug="a/b", failures=failures)
        assert "diagnose the root cause before writing" in prompt
        assert "assert BEHAVIOR" in prompt
        assert "coverage-theater" in prompt
        assert "# pragma: no cover" in prompt
        assert "exact threshold" in prompt
        assert "a justified " in prompt
        assert "non-behavioral" in prompt
        assert "Protocol stub" in prompt
