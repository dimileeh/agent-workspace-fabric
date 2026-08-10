"""Focused ready-to-merge comment rendering regressions."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_prompts import ready_to_merge_comment


@pytest.mark.unit
def test_blocker_items_escape_untrusted_markdown_and_link_destinations() -> None:
    """Verify blocker items escape untrusted markdown and link destinations."""
    body = ready_to_merge_comment(
        pr_number=1,
        head_sha="a" * 40,
        blocker_reason="review feedback needs human input",
        blocker_items=(
            {
                "kind": "thread",
                "id": "T1",
                "author": "review-bot[bot]",
                "path": "src/[critical].py\n## forged heading",
                "line": 42,
                "url": "https://github.example/reviews/T1)\n[forged](https://evil.example)",
                "body": "Read [this](https://evil.example)\n## forged body ~~struck excerpt~~",
                "verdict": "needs_human",
                "agent_verdict_reason": "Use `safe`\n- forged reason ~~struck reason~~",
            },
            {
                "kind": "review",
                "id": "R1",
                "author": "alice\n## forged author",
                "path": None,
                "line": None,
                "url": "https://github.example/reviews/R1",
                "body": "review-level feedback",
                "verdict": "defer",
                "agent_verdict_reason": None,
            },
        ),
    )

    assert "src/\\[critical\\].py \\#\\# forged heading:42" in body
    assert (
        "Read \\[this\\]\\(https://evil.example\\) \\#\\# forged body \\~\\~struck excerpt\\~\\~"
        in body
    )
    assert "-> reason: Use \\`safe\\` - forged reason \\~\\~struck reason\\~\\~" in body
    assert "[alice \\#\\# forged author](https://github.example/reviews/R1)" in body
    assert "https://github.example/reviews/T1%29%0A%5Bforged%5D%28https://evil.example%29" in body
    assert "\n## forged" not in body
    assert "\n- forged" not in body
    assert "[forged](https://evil.example)" not in body


@pytest.mark.unit
def test_blocker_items_neutralize_mentions_in_untrusted_excerpt_and_reason() -> None:
    """Verify blocker items neutralize mentions in untrusted excerpt and reason."""
    body = ready_to_merge_comment(
        pr_number=1,
        head_sha="a" * 40,
        blocker_reason="review feedback needs human input",
        blocker_items=(
            {
                "kind": "thread",
                "id": "T1",
                "author": "review-bot[bot]",
                "path": "src/monitor.py",
                "line": 42,
                "url": "https://github.example/reviews/T1",
                "body": "Please ask @reviewer and @acme/security.",
                "verdict": "needs_human",
                "agent_verdict_reason": "Confirm with @maintainer first.",
            },
        ),
    )

    assert "&#64;reviewer" in body
    assert "&#64;acme/security" in body
    assert "&#64;maintainer" in body
    assert "@reviewer" not in body
    assert "@acme/security" not in body
    assert "@maintainer" not in body
