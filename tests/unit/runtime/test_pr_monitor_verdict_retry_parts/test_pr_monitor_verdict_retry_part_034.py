"""A baseline-outage park keeps the comment repair's work for the resume.

When the commit sink's protected-scope validation cannot fetch the PR-branch
baseline after its bounded retries, it raises the transient
``ProtectedScopeBaselineUnavailableError`` and the fix cycle parks the workspace
in ``blocked``, promising to keep the unpushed repair (#1221). The verdict
protocol used to roll the item back first, so the park recorded the rolled-back
HEAD and the resume had nothing to continue (PRRT_kwDOSJAM6s63N-C1). The repair's
commits and edits now stay, and the stranded-edit marker and evidence are
persisted before the error reaches the park, as the CI repair does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_baseline_park
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeBaselineUnavailableError,
    ProtectedScopeDiffError,
)
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_REPAIR_HEAD = "b" * 40


def _runner(tmp_path: Path, exc: ProtectedScopeDiffError) -> _VerdictRunner:
    """A FIXED repair whose commit sink raises ``exc`` with the edit still dirty."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    (worktree / "fix.py").write_text("fixed = True\n")
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[_REPAIR_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD
    runner.persisted = []

    async def _raise_during_commit(**_kwargs: object) -> bool:
        runner.current_head = _REPAIR_HEAD
        raise exc

    async def _dirty_result(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="dirty",
            reason_code="PRE_EXISTING_DIRTY_WORKTREE",
            details={"paths": ["fix.py"]},
        )

    async def _persist_state(workspace_id: str, state: MonitorState) -> None:
        runner.persisted.append((workspace_id, dict(state.threads_addressed_ids)))

    runner._commit_dirty_worktree = _raise_during_commit
    runner._pre_existing_dirty_repair_worktree_result = _dirty_result
    runner._persist_state = _persist_state
    return runner


async def _invoke(runner: _VerdictRunner, state: MonitorState) -> None:
    await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=_ITEM_START_HEAD,
    )


@pytest.mark.unit
async def test_baseline_outage_in_commit_sink_keeps_repair_for_park(tmp_path: Path) -> None:
    exc = ProtectedScopeBaselineUnavailableError("PR branch fetch failed: DNS")
    runner = _runner(tmp_path, exc)
    state = MonitorState()

    with pytest.raises(ProtectedScopeBaselineUnavailableError) as raised:
        await _invoke(runner, state)

    assert raised.value is exc
    # No rollback: the park preserves the repair it reports as kept.
    assert runner.reset_targets == []
    assert runner.current_head == _REPAIR_HEAD
    # The guide resume commits exactly the edits the sink left behind.
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _REPAIR_HEAD
    evidence = json.loads(
        state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY]
    )
    assert list(evidence) == ["fix.py"]
    assert evidence["fix.py"] is not None
    # Persisted ahead of the park, so a restart cannot lose the marker.
    assert len(runner.persisted) == 1
    workspace_id, persisted = runner.persisted[0]
    assert workspace_id == "ws_protocol"
    assert persisted[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _REPAIR_HEAD


@pytest.mark.unit
async def test_baseline_outage_on_hosted_adapter_still_rolls_back(tmp_path: Path) -> None:
    """A hosted agent publishes before gating, so its ungated push is still rewound."""
    runner = _runner(tmp_path, ProtectedScopeBaselineUnavailableError("PR branch fetch failed"))
    runner._deps.adapter.is_hosted = True
    state = MonitorState()

    with pytest.raises(ProtectedScopeBaselineUnavailableError):
        await _invoke(runner, state)

    assert runner.reset_targets[0] == _ITEM_START_HEAD
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids
    assert runner.persisted == []


@pytest.mark.unit
async def test_non_transient_protected_scope_diff_still_rolls_back(tmp_path: Path) -> None:
    runner = _runner(tmp_path, ProtectedScopeDiffError("protected-scope diff unavailable"))
    state = MonitorState()

    with pytest.raises(ProtectedScopeDiffError):
        await _invoke(runner, state)

    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids
    assert runner.persisted == []


@pytest.mark.unit
async def test_baseline_outage_with_unreadable_head_still_keeps_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable HEAD is recorded empty; the repair is still never rolled back."""
    runner = _runner(tmp_path, ProtectedScopeBaselineUnavailableError("PR branch fetch failed"))
    state = MonitorState()

    async def _unreadable_head(*_args: object, **_kwargs: object) -> str | None:
        raise OSError("git spawn failed")

    monkeypatch.setattr(
        comment_verdict_baseline_park, "read_protocol_attempt_start_head", _unreadable_head
    )

    with pytest.raises(ProtectedScopeBaselineUnavailableError):
        await _invoke(runner, state)

    assert runner.reset_targets == []
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == ""
    assert len(runner.persisted) == 1
