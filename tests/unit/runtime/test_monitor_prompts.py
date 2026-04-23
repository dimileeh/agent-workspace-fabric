"""Tests for the PR-monitor CLI prompt templates."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
    fix_ci_prompt,
    ready_to_merge_comment,
    sync_base_conflict_prompt,
)
from awf.runtime.pr_monitor import CheckFailure, ReviewComment, ReviewThread


class TestAddressThread:
    @pytest.mark.unit
    def test_embeds_pr_number_repo_file_line_thread_id(self) -> None:
        thread = ReviewThread(
            thread_id="PRRT_abc",
            path="src/app/api/projects/route.ts",
            line=42,
            body_excerpt="rename this handler",
            author="coderabbit",
        )
        prompt = address_thread_prompt(pr_number=99, repo_slug="dimileeh/aira-web", thread=thread)
        assert "#99" in prompt
        assert "dimileeh/aira-web" in prompt
        assert "src/app/api/projects/route.ts" in prompt
        assert "line 42" in prompt
        assert "PRRT_abc" in prompt
        assert "rename this handler" in prompt
        assert "coderabbit" in prompt

    @pytest.mark.unit
    def test_forbids_push_in_footer(self) -> None:
        thread = ReviewThread(thread_id="T", path="x", line=1, body_excerpt="")
        prompt = address_thread_prompt(pr_number=1, repo_slug="a/b", thread=thread)
        assert "Do NOT push" in prompt

    @pytest.mark.unit
    def test_prescribes_three_verdict_shapes(self) -> None:
        thread = ReviewThread(thread_id="T", path="x", line=1, body_excerpt="")
        prompt = address_thread_prompt(pr_number=1, repo_slug="a/b", thread=thread)
        assert "FALSE POSITIVE:" in prompt
        assert "DEFER:" in prompt
        assert "fixed in commit" in prompt

    @pytest.mark.unit
    def test_handles_missing_file_anchor_gracefully(self) -> None:
        thread = ReviewThread(thread_id="T", path=None, line=None, body_excerpt="x")
        prompt = address_thread_prompt(pr_number=1, repo_slug="a/b", thread=thread)
        assert "inside the file under review" in prompt


class TestAddressReviewComment:
    @pytest.mark.unit
    def test_embeds_identifiers_and_body(self) -> None:
        c = ReviewComment(
            comment_id="C_42",
            body_excerpt="summary: rename helpers",
            author="coderabbit",
        )
        prompt = address_review_comment_prompt(pr_number=99, repo_slug="x/y", comment=c)
        assert "#99" in prompt
        assert "x/y" in prompt
        assert "C_42" in prompt
        assert "summary: rename helpers" in prompt
        assert "coderabbit" in prompt

    @pytest.mark.unit
    def test_mentions_gh_pr_comment_for_reply(self) -> None:
        c = ReviewComment(comment_id="C", body_excerpt="x")
        prompt = address_review_comment_prompt(pr_number=1, repo_slug="a/b", comment=c)
        assert "gh pr comment" in prompt

    @pytest.mark.unit
    def test_forbids_push(self) -> None:
        c = ReviewComment(comment_id="C", body_excerpt="")
        prompt = address_review_comment_prompt(pr_number=1, repo_slug="a/b", comment=c)
        assert "Do NOT push" in prompt


class TestSyncBaseConflictPrompt:
    @pytest.mark.unit
    def test_names_base_and_lists_conflicting_files(self) -> None:
        prompt = sync_base_conflict_prompt(
            pr_number=7,
            repo_slug="a/b",
            base_branch="development",
            conflicting_files=("src/x.ts", "src/y.ts"),
        )
        assert "#7" in prompt
        assert "a/b" in prompt
        assert "`development`" in prompt
        assert "src/x.ts" in prompt
        assert "src/y.ts" in prompt

    @pytest.mark.unit
    def test_empty_conflicting_files_tells_agent_to_use_git_status(self) -> None:
        prompt = sync_base_conflict_prompt(
            pr_number=1, repo_slug="a/b", base_branch="main", conflicting_files=()
        )
        assert "run git status" in prompt


class TestFixCiPrompt:
    @pytest.mark.unit
    def test_includes_every_failure_name_and_conclusion(self) -> None:
        failures = (
            CheckFailure(name="playwright", conclusion="FAILURE", log_excerpt="err1"),
            CheckFailure(name="lint", conclusion="TIMED_OUT", log_excerpt="err2"),
        )
        prompt = fix_ci_prompt(pr_number=1, repo_slug="a/b", failures=failures)
        assert "playwright" in prompt
        assert "FAILURE" in prompt
        assert "lint" in prompt
        assert "TIMED_OUT" in prompt
        assert "err1" in prompt
        assert "err2" in prompt

    @pytest.mark.unit
    def test_forbids_skipping_or_disabling_checks(self) -> None:
        failures = (CheckFailure(name="a", conclusion="FAILURE", log_excerpt=""),)
        prompt = fix_ci_prompt(pr_number=1, repo_slug="a/b", failures=failures)
        assert "Do not disable" in prompt

    @pytest.mark.unit
    def test_falls_back_when_failures_empty(self) -> None:
        prompt = fix_ci_prompt(pr_number=1, repo_slug="a/b", failures=())
        assert "gh run list" in prompt

    @pytest.mark.unit
    def test_marks_missing_log_as_not_available(self) -> None:
        failures = (CheckFailure(name="a", conclusion="FAILURE", log_excerpt=""),)
        prompt = fix_ci_prompt(pr_number=1, repo_slug="a/b", failures=failures)
        assert "(no log available)" in prompt


class TestReadyToMergeComment:
    @pytest.mark.unit
    def test_includes_pr_number_and_short_sha(self) -> None:
        body = ready_to_merge_comment(pr_number=42, head_sha="1234567890abcdef")
        assert "#42" in body
        assert "`1234567890`" in body

    @pytest.mark.unit
    def test_lists_all_five_gates(self) -> None:
        body = ready_to_merge_comment(pr_number=1, head_sha="a" * 40)
        for gate in [
            "Inline comments resolved",
            "Outside-diff comments",
            "CI checks all SUCCESS",
            "Mergeable",
            "Base merged into head",
        ]:
            assert gate in body

    @pytest.mark.unit
    def test_states_human_action_required(self) -> None:
        body = ready_to_merge_comment(pr_number=1, head_sha="a" * 40)
        assert "human action required" in body.lower()
