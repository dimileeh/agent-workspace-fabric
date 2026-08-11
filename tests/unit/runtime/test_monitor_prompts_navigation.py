"""Focused base-conflict and operator-hint prompt regressions."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_prompts import operator_hint_prompt, sync_base_conflict_prompt


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

    @pytest.mark.unit
    def test_includes_trusted_workspace_runtime_context(self) -> None:
        prompt = sync_base_conflict_prompt(
            pr_number=1,
            repo_slug="a/b",
            base_branch="main",
            conflicting_files=("src/app.py",),
            workspace_runtime_context="Workspace runtime context\n- Sidecar services are running.",
        )

        assert "Workspace runtime context" in prompt
        assert "Sidecar services are running" in prompt
        assert (
            "AWF just ran `git merge origin/main` and it stopped on conflicts in these files:"
            in prompt
        )
        assert (
            "AWF just ran `git merge origin/main` and it stopped on conflicts in these files:\n\n"
            "  - src/app.py\n\n"
            "Workspace runtime context\n"
            "- Sidecar services are running.\n\n"
            "Resolve each conflict" in prompt
        )


class TestOperatorHintPrompt:
    @pytest.mark.unit
    def test_operator_hint_is_injected_as_untrusted_repair_evidence(self) -> None:
        prompt = operator_hint_prompt(
            pr_number=307,
            repo_slug="dimileeh/awf",
            reason="the docs CTA URL 404s; correct URL is https://example.test/docs",
            operation_id="op_rehint",
            workspace_runtime_context="Workspace runtime context\n- Service `postgres`: use postgres:5432.",
        )

        assert (
            "An operator manually provided guidance for this PR with the following hint:" in prompt
        )
        assert "Address what the hint says, commit any code changes locally" in prompt
        assert "push a fix commit" not in prompt
        assert "reply to any relevant unresolved review threads" in prompt
        assert "op_rehint" in prompt
        assert "Workspace runtime context" in prompt
        assert "UNTRUSTED EXTERNAL EVIDENCE" in prompt
        assert "source_kind: operator_hint" in prompt
        assert (
            "AWF-EVIDENCE> the docs CTA URL 404s; correct URL is https://example.test/docs"
        ) in prompt
        assert "Do NOT push" in prompt

    @pytest.mark.unit
    def test_directive_is_rendered_as_the_repair_evidence(self) -> None:
        prompt = operator_hint_prompt(
            pr_number=443,
            repo_slug="dimileeh/awf",
            reason="operator guidance recorded",
            directive="implement the forge-neutral fix, do not defer",
            operation_id="op_guide",
        )

        assert ("AWF-EVIDENCE> implement the forge-neutral fix, do not defer") in prompt
        assert "AWF-EVIDENCE> operator guidance recorded" not in prompt

    @pytest.mark.unit
    def test_directive_absent_falls_back_to_reason(self) -> None:
        prompt = operator_hint_prompt(
            pr_number=443,
            repo_slug="dimileeh/awf",
            reason="reply to the relevant unresolved review thread",
        )

        assert "AWF-EVIDENCE> reply to the relevant unresolved review thread" in prompt

    @pytest.mark.unit
    def test_prescribes_fixed_verdict_for_successful_code_or_no_code_hints(self) -> None:
        prompt = operator_hint_prompt(
            pr_number=329,
            repo_slug="dimileeh/awf",
            reason="reply to the relevant unresolved review thread without code changes",
        )

        assert "AWF-VERDICT: FIXED:" in prompt
        assert "code changes or only no-code" in prompt
