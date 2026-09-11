"""The #932 retry anchor is bound to the feedback body it answered (#934 audit).

The preserved item-start marker is keyed by the item id alone, and a review
comment's id survives an edit while a thread's id survives a new reply. So a
reviewer who changes the feedback after the timeout but before the retry keeps
the key: the retry restored the old start HEAD and the commits the timed-out
attempt made for the *previous* body counted as ``FIXED`` evidence for the new
one — the agent could re-affirm FIXED without touching the changed request.

The marker now carries the body hash it was written for. A definitive mismatch
drops the anchor (and does not re-arm it), so the retry falls back to its own
start HEAD and must earn its evidence; an unknown hash on either side proves
nothing and keeps the #932 concession intact.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner import comment_verdict_timeout_preserve as timeout_preserve
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictExecutionError
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_body_hash_changed,
    item_start_head_state_key,
    peek_item_start_body_hash,
    peek_item_start_head,
    remember_item_start_head,
)
from tests.unit.runtime._verdict_retry_fixtures import _agent_error, _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40
_REATTEMPT_HEAD = "c" * 40
_ITEM_ID = "issue:5558086911"
_ORIGINAL_BODY_HASH = "1" * 64
_EDITED_BODY_HASH = "2" * 64


def _timeout_error() -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="command idle timeout\n"),
        reason_code="AGENT_IDLE_TIMEOUT",
    )


class _EvidenceAnchorRunner(_VerdictRunner):
    """Verdict runner that records the anchor each evidence probe measures from."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.evidence_ancestors: list[str] = []

    async def _head_descends_from(
        self,
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        self.evidence_ancestors.append(ancestor)
        return await super()._head_descends_from(
            worktree_path=worktree_path,
            ancestor=ancestor,
            descendant=descendant,
        )


def _state_after_preserved_timeout(body_hash: str | None = _ORIGINAL_BODY_HASH) -> MonitorState:
    """The state #932 leaves behind: the original item start and its body hash."""
    state = MonitorState()
    remember_item_start_head(state, _ITEM_ID, _ITEM_START_HEAD, body_hash)
    return state


async def _invoke_reattempt(
    runner: _VerdictRunner,
    *,
    state: MonitorState,
    body_hash: str | None = _ORIGINAL_BODY_HASH,
) -> comment_verdict.VerdictResult:
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message=f"fix: address PR review comment {_ITEM_ID}",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        # What the next monitor pass passes in: the live (preserved) HEAD.
        operation_start_head=_PRESERVED_HEAD,
        evidence_item_id=_ITEM_ID,
        evidence_body_hash=body_hash,
    )


def _fixed_reattempt_runner(tmp_path: Path) -> _EvidenceAnchorRunner:
    runner = _EvidenceAnchorRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: the preserved commit already covers this"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    return runner


@pytest.mark.unit
async def test_edited_feedback_drops_the_preserved_anchor(tmp_path: Path) -> None:
    """Changed body: the retry measures FIXED from its own start, not the old one."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _fixed_reattempt_runner(tmp_path)
    state = _state_after_preserved_timeout()

    with structlog.testing.capture_logs() as captured:
        result = await _invoke_reattempt(runner, state=state, body_hash=_EDITED_BODY_HASH)

    assert result.verdict == "fix_committed"
    assert runner.evidence_ancestors == [_PRESERVED_HEAD]
    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids
    body_changed = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_item_start_head_body_changed"
    ]
    assert len(body_changed) == 1
    assert body_changed[0]["item_start_head"] == _ITEM_START_HEAD
    assert body_changed[0]["attempt_start_head"] == _PRESERVED_HEAD


@pytest.mark.unit
async def test_unchanged_feedback_keeps_the_preserved_anchor(tmp_path: Path) -> None:
    """The #932 concession stands while the reviewer's request is the same one."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _fixed_reattempt_runner(tmp_path)
    state = _state_after_preserved_timeout()

    result = await _invoke_reattempt(runner, state=state)

    assert result.verdict == "fix_committed"
    assert runner.evidence_ancestors == [_ITEM_START_HEAD]


@pytest.mark.unit
@pytest.mark.parametrize("current_body_hash", [_ORIGINAL_BODY_HASH, None])
async def test_a_marker_without_a_body_hash_still_anchors(
    tmp_path: Path,
    current_body_hash: str | None,
) -> None:
    """An unknown hash proves nothing, so the legacy marker keeps its anchor."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _fixed_reattempt_runner(tmp_path)
    state = _state_after_preserved_timeout(body_hash=None)

    result = await _invoke_reattempt(runner, state=state, body_hash=current_body_hash)

    assert result.verdict == "fix_committed"
    assert runner.evidence_ancestors == [_ITEM_START_HEAD]


@pytest.mark.unit
async def test_a_body_changed_anchor_is_not_re_armed_by_a_failed_attempt(
    tmp_path: Path,
) -> None:
    """Dropping is permanent: the failure path must not resurrect a stale anchor."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _EvidenceAnchorRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    with pytest.raises(AgentVerdictExecutionError):
        await _invoke_reattempt(runner, state=state, body_hash=_EDITED_BODY_HASH)

    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_a_re_armed_anchor_keeps_its_body_binding(
    tmp_path: Path,
) -> None:
    """An attempt that dies before a verdict puts the binding back, not a bare SHA."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _EvidenceAnchorRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    with pytest.raises(AgentVerdictExecutionError):
        await _invoke_reattempt(runner, state=state)

    assert peek_item_start_head(state, _ITEM_ID) == _ITEM_START_HEAD
    assert peek_item_start_body_hash(state, _ITEM_ID) == _ORIGINAL_BODY_HASH


@pytest.mark.unit
async def test_a_timeout_binds_the_marker_to_the_current_feedback_body(
    tmp_path: Path,
) -> None:
    """The preserve path records the body its work answered, not just the HEAD."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_timeout_error()],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    state = MonitorState()

    with pytest.raises(AgentVerdictExecutionError):
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message=f"fix: address PR review comment {_ITEM_ID}",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=_ITEM_START_HEAD,
            evidence_item_id=_ITEM_ID,
            evidence_body_hash=_ORIGINAL_BODY_HASH,
        )

    assert peek_item_start_head(state, _ITEM_ID) == _ITEM_START_HEAD
    assert peek_item_start_body_hash(state, _ITEM_ID) == _ORIGINAL_BODY_HASH


@pytest.mark.unit
@pytest.mark.parametrize(
    ("recorded", "current", "expected"),
    [
        (_ORIGINAL_BODY_HASH, _EDITED_BODY_HASH, True),
        (_ORIGINAL_BODY_HASH, _ORIGINAL_BODY_HASH, False),
        (None, _EDITED_BODY_HASH, False),
        (_ORIGINAL_BODY_HASH, None, False),
        (None, None, False),
    ],
)
def test_item_start_body_hash_changed_gate(
    recorded: str | None,
    current: str | None,
    expected: bool,
) -> None:
    assert item_start_body_hash_changed(recorded, current) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (None, None)),
        ("", (None, None)),
        # Written before the binding existed (or by a caller with no body hash).
        (_ITEM_START_HEAD, (None, _ITEM_START_HEAD)),
        (f"{_ORIGINAL_BODY_HASH}:{_ITEM_START_HEAD}", (_ORIGINAL_BODY_HASH, _ITEM_START_HEAD)),
        # Truncated marker: no HEAD means no anchor, never a hash used as one.
        (f"{_ORIGINAL_BODY_HASH}:", (_ORIGINAL_BODY_HASH, None)),
    ],
)
def test_marker_decoding(raw: str | None, expected: tuple[str | None, str | None]) -> None:
    assert timeout_preserve._decode_item_start_marker(raw) == expected
