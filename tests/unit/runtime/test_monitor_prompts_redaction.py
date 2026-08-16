"""Monitor prompt redaction regressions (split for line-limit maintainability)."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_prompts import ready_to_merge_comment


class TestReadyToMergeCommentRedaction:
    @pytest.mark.unit
    def test_blocker_item_excerpt_redacts_url_credentials_before_truncating(self) -> None:
        """Verify blocker item excerpt redacts url credentials before truncating."""
        password = "credential-that-crosses-the-boundary"
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
                    "body": f"{'x' * 130} https://username:{password}@example.com/details",
                    "verdict": "needs_human",
                    "agent_verdict_reason": None,
                },
            ),
        )

        assert password[:12] not in body
        assert r"\<redacted\>" in body
        assert "<redacted>" not in body
