"""Feedback triage runner wires applied operator decisions into prompts (#1283)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

from awf.common.github_client import RepoRef
from awf.common.prompt_evidence import EVIDENCE_QUOTE_PREFIX
from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
)
from awf.runtime.pr_monitor import MonitorState, OperatorHint
from awf.runtime.pr_monitor_models import ReviewComment, ReviewThread
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comment_verdict import VerdictResult
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint,
)

_REPO = RepoRef(owner="dimileeh", name="awf")
_THREAD = ReviewThread(
    thread_id="PRRT_kwDOSJAM6s6ftriage",
    path="src/awf/runtime/pr_monitor_runner/comments.py",
    line=205,
    body_excerpt="this finding should be deferred",
    author="reviewer",
)
_COMMENT = ReviewComment(
    comment_id="RC_1283",
    body_excerpt="architecture remark that was deferred",
    author="reviewer",
)
_DIRECTIVE_TEXT = "Defer thread PRRT_kwDOSJAM6s6ftriage to #999; add no code."
_OPERATION_ID = "op-guide-1283"
_HEAD_SHA = "b34a78bf86abcdef0123456789abcdef01234567"


def _seed_directive_via_guide_finalize(
    state: MonitorState, *, operation_id: str = _OPERATION_ID
) -> None:
    """Record the decision the way AddressOperatorHint completion does (#1268)."""
    hint = OperatorHint(
        reason="operator guide",
        directive=_DIRECTIVE_TEXT,
        operation_id=operation_id,
        reason_code="OPERATOR_GUIDE",
    )
    state.pending_operator_hint = hint
    _finalize_processed_operator_hint(
        state,
        hint=hint,
        acted_feedback_text=hint.directive,
        head_sha=_HEAD_SHA,
    )


def _runner() -> SimpleNamespace:
    async def _invoke(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="false_positive")

    return SimpleNamespace(
        _workspace_runtime_context="",
        _invoke_cli_for_verdict_result=_invoke,
    )


@pytest.mark.unit
async def test_address_thread_prompt_contains_applied_directive_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built_prompts: list[str] = []
    original = comments.address_thread_prompt

    def _capture(**kwargs: object) -> str:
        prompt = original(**kwargs)
        built_prompts.append(prompt)
        return prompt

    monkeypatch.setattr(comments, "address_thread_prompt", _capture)
    state = MonitorState()
    _seed_directive_via_guide_finalize(state)

    with structlog.testing.capture_logs() as captured:
        await comments._address_thread(
            _runner(),
            workspace_id="ws_1283",
            repo=_REPO,
            pr_number=1283,
            thread=_THREAD,
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            owned_paths=["src/"],
            task_tag=None,
        )

    assert built_prompts
    prompt = built_prompts[0]
    assert _DIRECTIVE_TEXT in prompt
    assert _OPERATION_ID in prompt
    assert "does not restrict the triage" in prompt.lower()
    assert [line for line in prompt.splitlines() if _DIRECTIVE_TEXT in line] == [
        f"{EVIDENCE_QUOTE_PREFIX}{_DIRECTIVE_TEXT}"
    ]

    audit_events = [
        event
        for event in captured
        if event.get("event") == "monitor.feedback_triage_applied_operator_directives"
    ]
    assert len(audit_events) == 1
    assert audit_events[0]["count"] == 1
    assert audit_events[0]["operation_ids"] == [_OPERATION_ID]
    assert audit_events[0]["workspace_id"] == "ws_1283"
    assert audit_events[0]["pr_number"] == 1283
    assert audit_events[0]["thread_id"] == _THREAD.thread_id
    assert "directive" not in audit_events[0]
    assert _DIRECTIVE_TEXT not in str(audit_events[0])


@pytest.mark.unit
async def test_address_review_comment_prompt_contains_applied_directive_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built_prompts: list[str] = []
    original = comments.address_review_comment_prompt

    def _capture(**kwargs: object) -> str:
        prompt = original(**kwargs)
        built_prompts.append(prompt)
        return prompt

    monkeypatch.setattr(comments, "address_review_comment_prompt", _capture)
    state = MonitorState()
    _seed_directive_via_guide_finalize(state)

    with structlog.testing.capture_logs() as captured:
        await comments._address_review_comment_result(
            _runner(),
            workspace_id="ws_1283",
            repo=_REPO,
            pr_number=1283,
            comment=_COMMENT,
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            owned_paths=["src/"],
            task_tag=None,
        )

    assert built_prompts
    prompt = built_prompts[0]
    assert _DIRECTIVE_TEXT in prompt
    assert _OPERATION_ID in prompt
    assert "does not restrict the triage" in prompt.lower()

    audit_events = [
        event
        for event in captured
        if event.get("event") == "monitor.feedback_triage_applied_operator_directives"
    ]
    assert len(audit_events) == 1
    assert audit_events[0]["count"] == 1
    assert audit_events[0]["operation_ids"] == [_OPERATION_ID]
    assert audit_events[0]["workspace_id"] == "ws_1283"
    assert audit_events[0]["pr_number"] == 1283
    assert audit_events[0]["comment_id"] == _COMMENT.comment_id
    assert "directive" not in audit_events[0]
    assert _DIRECTIVE_TEXT not in str(audit_events[0])


@pytest.mark.unit
async def test_address_thread_without_state_matches_baseline_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built_prompts: list[str] = []
    original = comments.address_thread_prompt

    def _capture(**kwargs: object) -> str:
        prompt = original(**kwargs)
        built_prompts.append(prompt)
        return prompt

    monkeypatch.setattr(comments, "address_thread_prompt", _capture)
    baseline = address_thread_prompt(
        pr_number=1283,
        repo_slug=_REPO.slug(),
        thread=_THREAD,
        workspace_runtime_context="",
        owned_paths=["src/"],
        task_tag=None,
    )

    with structlog.testing.capture_logs() as captured:
        await comments._address_thread(
            _runner(),
            workspace_id="ws_1283",
            repo=_REPO,
            pr_number=1283,
            thread=_THREAD,
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=None,
            owned_paths=["src/"],
            task_tag=None,
        )

    assert built_prompts == [baseline]
    assert not any(
        event.get("event") == "monitor.feedback_triage_applied_operator_directives"
        for event in captured
    )


@pytest.mark.unit
async def test_address_review_comment_without_state_matches_baseline_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built_prompts: list[str] = []
    original = comments.address_review_comment_prompt

    def _capture(**kwargs: object) -> str:
        prompt = original(**kwargs)
        built_prompts.append(prompt)
        return prompt

    monkeypatch.setattr(comments, "address_review_comment_prompt", _capture)
    baseline = address_review_comment_prompt(
        pr_number=1283,
        repo_slug=_REPO.slug(),
        comment=_COMMENT,
        workspace_runtime_context="",
        owned_paths=["src/"],
        task_tag=None,
    )

    with structlog.testing.capture_logs() as captured:
        await comments._address_review_comment_result(
            _runner(),
            workspace_id="ws_1283",
            repo=_REPO,
            pr_number=1283,
            comment=_COMMENT,
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=None,
            owned_paths=["src/"],
            task_tag=None,
        )

    assert built_prompts == [baseline]
    assert not any(
        event.get("event") == "monitor.feedback_triage_applied_operator_directives"
        for event in captured
    )


@pytest.mark.unit
async def test_address_thread_with_empty_store_emits_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State present but no applied records → old prompt, no triage audit."""
    prompt_kwargs: list[dict[str, Any]] = []
    original = comments.address_thread_prompt

    def _capture(**kwargs: object) -> str:
        prompt_kwargs.append(dict(kwargs))
        return original(**kwargs)

    monkeypatch.setattr(comments, "address_thread_prompt", _capture)
    state = MonitorState()

    with structlog.testing.capture_logs() as captured:
        await comments._address_thread(
            _runner(),
            workspace_id="ws_1283",
            repo=_REPO,
            pr_number=1283,
            thread=_THREAD,
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            owned_paths=["src/"],
            task_tag=None,
        )

    assert prompt_kwargs
    assert (
        prompt_kwargs[0].get("applied_operator_directives") in ((), None)
        or list(prompt_kwargs[0].get("applied_operator_directives") or ()) == []
    )
    assert not any(
        event.get("event") == "monitor.feedback_triage_applied_operator_directives"
        for event in captured
    )
