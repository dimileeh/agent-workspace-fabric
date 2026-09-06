"""Correction-attempt path-level FIXED acceptance (#925 D1 restored on top of #928).

Attempt 0 keeps the strict line-anchored evidence gate, so a FIXED whose hunks
land away from the anchored line still earns its correction round. On the
correction attempt the agent has been told, explicitly, that its FIXED carried
no line evidence — if it re-affirms FIXED and the item's *own* commit range
(``item_start_head..HEAD``) is a contentful descendant that changes the reviewed
file, that is an honest off-anchor fix, not an unsupported claim. Accept it as
``fix_committed`` instead of escalating an honest fix to a human.

The narrowing conditions are what make this safe and are pinned below: it is
never the first gate (attempt 0 stays strict), the probe runs over the item's
own commit range so the commit cannot be stale or foreign, a commit that touches
none of the reviewed paths keeps #928's ``needs_human``-with-commit-preserved
disposition, the unmappable-anchor sentinel stays fail-closed, and an explicit
corrected ``NEEDS_HUMAN`` stays escalated (issue:5558086911).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import ReviewThread
from awf.runtime.pr_monitor_runner import (
    comment_verdict,
    comment_verdict_correction,
    comments,
)
from awf.runtime.pr_monitor_runner.comment_verdict import (
    _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT,
    AGENT_NON_FIX_CITES_OWN_COMMIT,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_ATTEMPT0_HEAD = "b" * 40
_REVIEWED_PATH = "src/awf/reviewed.py"


@pytest.fixture(autouse=True)
def _no_owned_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)


def _thread(thread_id: str) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path=_REVIEWED_PATH,
        line=42,
        body_excerpt="notification uses stale status",
    )


async def _address(runner: _VerdictRunner, thread: ReviewThread) -> str:
    return await _address_thread(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        repo=RepoRef(owner="o", name="r"),
        pr_number=1,
        thread=thread,
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=_ITEM_START_HEAD,
    )


@pytest.mark.unit
async def test_attempt_zero_never_accepts_path_level_evidence(tmp_path: Path) -> None:
    """Attempt 0 stays line-anchored: an off-anchor FIXED earns its correction.

    Path-level evidence is only ever consulted after the agent has been told its
    FIXED lacked line evidence, so the first attempt must still emit the
    correction prompt rather than resolving on path membership.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: fixed the reviewed file above the anchor",
            "AWF-VERDICT: FIXED: same change, the fix belongs above the anchor",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    verdict = await _address(runner, _thread("thread_attempt_zero_strict"))

    # Only the correction attempt resolves; attempt 0 was rejected first.
    assert verdict == "fix_committed"
    assert len(runner.prompts) == 2
    assert _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT in runner.prompts[1]
    assert "no new item-scoped Git change" in runner.prompts[1]
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD


@pytest.mark.unit
async def test_correction_path_level_acceptance_needs_the_anchored_path(
    tmp_path: Path,
) -> None:
    """A commit touching none of the reviewed paths keeps the #928 disposition."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: fixed the helper in a sibling module",
            "AWF-VERDICT: FIXED: still only the sibling module",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("thread_off_path_correction"))

    assert verdict == "needs_human"
    # Never fatal, never rolled back: the commit is preserved for the human.
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    events = [entry.get("event") for entry in captured]
    assert "monitor.agent_verdict_correction_fixed_outside_item_scope" in events


@pytest.mark.unit
async def test_self_citing_defer_with_path_level_evidence_returns_fixed(
    tmp_path: Path,
) -> None:
    """A self-citing DEFER on the correction resolves when path evidence holds.

    The correction prompt puts this item's own attempt-0 commit at HEAD, so the
    agent can answer ``DEFER: superseded by <its own sha>``. With correction-time
    path-level evidence for the reviewed file that commit *is* the fix.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: re-read the status before notifying",
            f"AWF-VERDICT: DEFER: superseded by commit {_ATTEMPT0_HEAD}",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("thread_self_citing_defer"))

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT
    assert self_citation[0]["verdict"] == "defer"
    assert self_citation[0]["has_path_evidence"] is True


@pytest.mark.unit
async def test_correction_needs_human_with_path_level_evidence_stays_needs_human(
    tmp_path: Path,
) -> None:
    """issue:5558086911: evidence must not override a requested human gate."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: re-read the status before notifying",
            f"AWF-VERDICT: NEEDS_HUMAN: addressed by {_ATTEMPT0_HEAD}, policy call needed",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("thread_corrected_needs_human"))

    assert verdict == "needs_human"
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["verdict"] == "needs_human"
    assert self_citation[0]["has_path_evidence"] is True


@pytest.mark.unit
async def test_path_level_probe_failure_rolls_back_under_the_sink_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path-level probe shares the commit sink's rollback handling.

    It runs inside the same ``try`` as the line-anchored check, so an ordinary
    Git failure rolls the unaccepted correction commit back to item start and
    propagates unmasked instead of stranding it in the worktree.
    """
    (tmp_path / "ws_protocol").mkdir()
    real_evidence = comment_verdict._item_fix_evidence

    async def _probe(runner: object, **kwargs: object) -> bool:
        if kwargs.get("item_path") is not None and kwargs.get("item_line") is None:
            raise OSError("git rev-parse spawn failed")
        return await real_evidence(runner, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict, "_item_fix_evidence", _probe)

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: fixed the reviewed file above the anchor",
            "AWF-VERDICT: FIXED: same change, the fix belongs above the anchor",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    with pytest.raises(OSError, match="git rev-parse spawn failed"):
        await _address(runner, _thread("thread_path_level_probe_raises"))

    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD


@pytest.mark.unit
async def test_path_level_item_fix_evidence_delegates_with_no_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper drops only the line anchor and resolves through ``comment_verdict``."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: unused"],
        heads_after_attempt=[_ATTEMPT0_HEAD],
    )
    seen: list[dict[str, object]] = []

    async def _probe(_runner: object, **kwargs: object) -> bool:
        seen.append(dict(kwargs))
        return True

    monkeypatch.setattr(comment_verdict, "_item_fix_evidence", _probe)

    assert await comment_verdict_correction.path_level_item_fix_evidence(
        runner,  # type: ignore[arg-type]
        worktree_path=worktree,
        item_start_head=_ITEM_START_HEAD,
        item_path=_REVIEWED_PATH,
        state=None,
        dirty_changes_committed=True,
    )

    assert seen == [
        {
            "worktree_path": worktree,
            "item_start_head": _ITEM_START_HEAD,
            "item_path": _REVIEWED_PATH,
            "item_line": None,
            "state": None,
            "dirty_changes_committed": True,
        }
    ]
