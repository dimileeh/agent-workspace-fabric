"""Focused blocker and task-tag monitor prompt tests."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
    fix_ci_prompt,
    operator_hint_prompt,
    ready_to_merge_comment,
    sync_base_conflict_prompt,
)
from awf.runtime.pr_monitor import CheckFailure, ReviewComment, ReviewThread


class TestReadyToMergeCommentIncidentReplay:
    @pytest.mark.unit
    def test_incident_replay_renders_all_reasonless_needs_human_items(self) -> None:
        """Verify incident replay renders all reasonless needs human items."""
        blocker_items = tuple(
            {
                "kind": "thread",
                "id": f"incident-{number}",
                "author": "review-bot[bot]",
                "path": f"src/incident_{number}.py",
                "line": number + 1,
                "url": f"https://github.example/reviews/incident-{number}",
                "body": f"incident blocker {number}",
                "verdict": "needs_human",
                "agent_verdict_reason": None,
            }
            for number in range(8)
        )

        body = ready_to_merge_comment(
            pr_number=46,
            head_sha="a" * 40,
            blocker_reason="review feedback needs human input and remains unresolved on GitHub",
            blocker_items=blocker_items,
        )

        assert "Agent escalated - needs your decision (8):" in body
        assert body.count("-> ⚠ no reason given by agent") == 8
        for number in range(8):
            assert f"incident blocker {number}" in body
        assert body != (
            "⚠️ PR #46 needs human attention at commit `aaaaaaaaaa`.\n\n"
            "AWF did not auto-merge because review feedback needs human input and remains "
            "unresolved on GitHub.\n\n"
            "After the blocker is cleared or a new commit lands, AWF will re-verify "
            "the PR before taking any merge action."
        )


class TestCommitTaskTagFooter:
    """Monitor prompts must instruct the agent to tag the commits it authors.

    For a tagged workspace the agent commits its own monitor fix (the worktree
    is clean, so AWF's ``_commit_dirty_worktree`` fallback never runs and never
    tags it). Without the prompt instruction the pushed monitor commit stays
    untagged and loses its Jira link (PRRT_kwDOSJAM6s6I9Tng).
    """

    _TAG = "PROJ-123"

    def _tagged_prompts(self, tag: str | None) -> list[str]:
        thread = ReviewThread(thread_id="T", path="x", line=1, body_excerpt="x")
        comment = ReviewComment(
            comment_id="c1", author="r", body="b", body_excerpt="b", state="COMMENTED"
        )
        failures = (CheckFailure(name="lint", conclusion="FAILURE", log_excerpt="x"),)
        return [
            address_thread_prompt(pr_number=1, repo_slug="a/b", thread=thread, task_tag=tag),
            address_review_comment_prompt(
                pr_number=1, repo_slug="a/b", comment=comment, task_tag=tag
            ),
            operator_hint_prompt(pr_number=1, repo_slug="a/b", reason="do x", task_tag=tag),
            sync_base_conflict_prompt(
                pr_number=1, repo_slug="a/b", base_branch="main", conflicting_files=(), task_tag=tag
            ),
            fix_ci_prompt(pr_number=1, repo_slug="a/b", failures=failures, task_tag=tag),
        ]

    @pytest.mark.unit
    def test_every_committing_prompt_instructs_tag_prefix_when_tag_present(self) -> None:
        for prompt in self._tagged_prompts(self._TAG):
            assert f"task tag `{self._TAG}`" in prompt
            assert "links to its tracking issue" in prompt
            assert "do not add it again" in prompt

    @pytest.mark.unit
    def test_every_committing_prompt_brackets_entity_task_tag(self) -> None:
        # Aira entity keys link only via the bracketed `[AIRA-T299] …` commit
        # form; the agent authors these monitor commits itself, so it must be
        # told the bracketed prefix, not the bare key (PRRT_kwDOSJAM6s6OHCyD).
        for prompt in self._tagged_prompts("AIRA-T299"):
            assert "task tag `[AIRA-T299]`" in prompt
            assert "`[AIRA-T299] fix: …`" in prompt
            assert "task tag `AIRA-T299`" not in prompt

    @pytest.mark.unit
    def test_no_tag_instruction_when_tag_absent(self) -> None:
        for prompt in self._tagged_prompts(None):
            assert "task tag" not in prompt
            assert "links to its tracking issue" not in prompt
            # The plain do-not-push footer is still present.
            assert "Do NOT push" in prompt

    @pytest.mark.unit
    def test_tag_instruction_defaults_off_for_backward_compatibility(self) -> None:
        thread = ReviewThread(thread_id="T", path="x", line=1, body_excerpt="x")
        prompt = address_thread_prompt(pr_number=1, repo_slug="a/b", thread=thread)
        assert "task tag" not in prompt
