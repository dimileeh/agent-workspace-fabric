"""Monitor prompt tests for review-thread bundles and verdict terminal records."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
    operator_hint_prompt,
)
from awf.runtime.pr_monitor import ReviewComment, ReviewThread, ReviewThreadComment


@pytest.mark.unit
def test_every_verdict_prompt_requires_a_terminal_record() -> None:
    thread = ReviewThread(thread_id="T", path="x", line=1, body_excerpt="x")
    comment = ReviewComment(comment_id="C", body_excerpt="x")
    prompts = (
        address_thread_prompt(pr_number=1, repo_slug="a/b", thread=thread),
        address_review_comment_prompt(pr_number=1, repo_slug="a/b", comment=comment),
        operator_hint_prompt(pr_number=1, repo_slug="a/b", reason="do x"),
    )

    for prompt in prompts:
        assert "final non-empty line of stdout" in prompt
        assert "Print nothing after that record; exit immediately." in prompt


@pytest.mark.unit
def test_review_bundle_prompt_quotes_independent_review_body_once() -> None:
    review_context = ReviewComment(
        comment_id="5000803010",
        body_excerpt="Independent review-body request.",
        body="Independent review-body request.",
        author="chatgpt-codex-connector",
        state="COMMENTED",
        url="https://github.example/reviews/5000803010",
    )
    thread = ReviewThread(
        thread_id="PRRT_bundle",
        path="src/awf/common/github_client.py",
        line=473,
        body_excerpt="Keep the inline finding.",
        comments=(
            ReviewThreadComment(
                comment_id="3836732990",
                body="Keep the inline finding.",
                author="chatgpt-codex-connector",
                review_id="5000803010",
            ),
        ),
        review_context=review_context,
    )

    prompt = address_thread_prompt(pr_number=862, repo_slug="dimileeh/awf", thread=thread)

    assert "one logical review bundle" in prompt
    assert prompt.count("AWF-EVIDENCE> Independent review-body request.") == 1
    assert "AWF-EVIDENCE> Associated review body:" in prompt
    assert "AWF-EVIDENCE> review_id: 5000803010" in prompt
    assert "AWF-EVIDENCE> Thread comment 1:" in prompt
