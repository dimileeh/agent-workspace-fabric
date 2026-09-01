"""Bounded correction-retry regressions (part 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import MonitorState, ReviewComment, ReviewThread
from awf.runtime.pr_monitor_runner import comment_verdict, comments
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictExecutionError,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
)
from tests.unit.runtime._verdict_retry_fixtures import (
    _agent_error,
    _invoke,
    _VerdictRunner,
)

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]


@pytest.mark.unit
async def test_protocol_violation_retries_same_prompt_once(tmp_path: Path) -> None:
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["decorated prose", "AWF-VERDICT: DEFER: track separately"],
        heads_after_attempt=["a" * 40, "a" * 40],
    )

    result = await _invoke(runner)

    assert result.verdict == "defer"
    assert result.reason == "track separately"
    assert len(runner.prompts) == 2
    assert runner.prompts[0] == "ORIGINAL REVIEW PROMPT"
    assert runner.prompts[1].startswith("ORIGINAL REVIEW PROMPT")
    assert "AWF-VERDICT: FIXED:" in runner.prompts[1]
    assert "final non-empty stdout line" in runner.prompts[1]


@pytest.mark.unit
async def test_second_protocol_violation_is_terminal_typed_error(tmp_path: Path) -> None:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["garbled one", "garbled two"],
        heads_after_attempt=["a" * 40, "a" * 40],
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_second_protocol_violation_rolls_back_hosted_commits_before_terminal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal protocol failure must rewind hosted PR heads, not strand unaccepted edits."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    synced_head = "b" * 40
    state = MonitorState(last_push_sha=item_start_head)
    remote_rollbacks: list[dict[str, object]] = []

    async def _record_remote_rollback(*args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["garbled one", "garbled two"],
        heads_after_attempt=[synced_head, synced_head],
        dirty_after_attempt=[True, False],
    )
    runner._deps.adapter.is_hosted = True

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=item_start_head,
            state=state,
        )

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 2
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head
    assert state.last_push_sha == item_start_head
    assert not state.hosted_terminal_head_advanced
    assert len(remote_rollbacks) == 1
    assert remote_rollbacks[0]["rollback_target_sha"] == item_start_head
    assert remote_rollbacks[0]["expected_remote_head_sha"] == synced_head


@pytest.mark.unit
async def test_fixed_without_evidence_gets_one_correction_then_fails(tmp_path: Path) -> None:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: claimed once",
            "AWF-VERDICT: FIXED: claimed twice",
        ],
        heads_after_attempt=["a" * 40, "a" * 40],
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_fixed_without_evidence_correction_explains_duplicate_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: already repaired for an earlier item",
            "AWF-VERDICT: FALSE POSITIVE: duplicate of an earlier repaired item",
        ],
        heads_after_attempt=["a" * 40, "a" * 40],
    )

    result = await _invoke(runner)

    assert result.verdict == "false_positive"
    assert len(runner.prompts) == 2
    assert "no new item-scoped Git change" in runner.prompts[1]
    assert "duplicate or was already addressed" in runner.prompts[1]


@pytest.mark.unit
async def test_protocol_retry_fixed_rejects_stale_first_attempt_evidence(
    tmp_path: Path,
) -> None:
    """FIXED on the correction attempt must not inherit evidence after HEAD reverts."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FIXED: claimed after reverting the bad commit",
        ],
        heads_after_attempt=[fixed_head, item_start_head],
        dirty_after_attempt=[True, False],
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=item_start_head,
        )

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_attempt_one_commit_supports_attempt_two_fixed(tmp_path: Path) -> None:
    (tmp_path / "ws_protocol").mkdir()
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing", "AWF-VERDICT: FIXED: committed the repair"],
        heads_after_attempt=[fixed_head, fixed_head],
        dirty_after_attempt=[True, False],
    )

    result = await _invoke(runner)

    assert result.verdict == "fix_committed"
    assert len(runner.prompts) == 2
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_first_attempt_non_fix_verdict_discards_committed_changes(
    tmp_path: Path,
) -> None:
    """A valid non-FIXED verdict on the first attempt must roll back committed edits."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FALSE POSITIVE: existing behavior is correct",
        ],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    result = await _invoke(runner)

    assert result.verdict == "false_positive"
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_protocol_retry_non_fix_verdict_discards_first_attempt_commits(
    tmp_path: Path,
) -> None:
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: duplicate of an earlier repaired item",
        ],
        heads_after_attempt=[fixed_head, fixed_head],
        dirty_after_attempt=[True, False],
    )

    result = await _invoke(runner)

    assert result.verdict == "false_positive"
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_correction_non_fixed_after_fixed_without_evidence_with_mutation_is_protocol_violation(
    tmp_path: Path,
) -> None:
    """Production regression: FIXED without evidence, then mutation + FALSE POSITIVE.

    Attempt 1 claims FIXED with no item-scoped evidence. The correction retry
    advances HEAD then reports FALSE POSITIVE. Rollback must restore the item
    start, and the non-FIXED verdict must not be returned.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    correction_head = "c" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: claimed without evidence",
            "AWF-VERDICT: FALSE POSITIVE: duplicate after editing on correction",
        ],
        heads_after_attempt=[item_start_head, correction_head],
        dirty_after_attempt=[False, True],
    )
    runner.current_head = item_start_head

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "non-FIXED" in str(caught.value).lower() or "correction" in str(caught.value).lower()
    assert len(runner.prompts) == 2
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
@pytest.mark.parametrize(
    ("correction_label",),
    [
        ("FALSE POSITIVE",),
        ("DEFER",),
        ("NEEDS_HUMAN",),
    ],
)
async def test_correction_non_fixed_with_head_advance_is_protocol_violation(
    tmp_path: Path,
    correction_label: str,
) -> None:
    """Correction attempt that commits/advances HEAD cannot accept non-FIXED."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    correction_head = "c" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            f"AWF-VERDICT: {correction_label}: contradiction after mutating on retry",
        ],
        heads_after_attempt=[attempt_one_head, correction_head],
        dirty_after_attempt=[True, True],
    )
    runner.current_head = item_start_head

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 2
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_correction_non_fixed_with_dirty_sink_without_head_advance_is_protocol_violation(
    tmp_path: Path,
) -> None:
    """Correction dirty_changes_committed with stable HEAD still fails closed.

    Models correction residue the sink reports as committed even when the
    fixture HEAD stays at attempt-start (reachable production signal without a
    separate porcelain probe).
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: claimed without evidence",
            "AWF-VERDICT: FALSE POSITIVE: dirty correction without head move",
        ],
        heads_after_attempt=[item_start_head, item_start_head],
        dirty_after_attempt=[False, True],
    )
    runner.current_head = item_start_head

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 2
    # No hard reset when HEAD already matches item start; cleanup may still run.
    assert runner.current_head == item_start_head


@pytest.mark.unit
@pytest.mark.parametrize(
    ("correction_label",),
    [
        ("FALSE POSITIVE",),
        ("DEFER",),
        ("NEEDS_HUMAN",),
    ],
)
async def test_correction_non_fixed_with_sink_false_stranded_dirty_is_protocol_violation(
    tmp_path: Path,
    correction_label: str,
) -> None:
    """Commit sink False with leftover dirt must not accept correction non-FIXED.

    Production regression for PRRT_kwDOSJAM6s6eILTO: when correction edits
    files but ``_commit_dirty_worktree`` returns False (status/add/commit
    failure), HEAD stays at attempt-start and ``dirty_changes_committed`` is
    False. Without a worktree probe before rollback, the contradictory
    non-FIXED verdict would be accepted after cleanup.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: claimed without evidence",
            f"AWF-VERDICT: {correction_label}: contradiction after stranded dirty edit",
        ],
        heads_after_attempt=[item_start_head, item_start_head],
        dirty_after_attempt=[False, False],
        stranded_dirty_after_attempt=[False, True],
    )
    runner.current_head = item_start_head

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "non-FIXED" in str(caught.value).lower() or "correction" in str(caught.value).lower()
    assert len(runner.prompts) == 2
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_correction_non_fixed_with_mutation_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Mutation + non-FIXED must fail closed when rollback itself cannot complete."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    correction_head = "c" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: claimed without evidence",
            "AWF-VERDICT: FALSE POSITIVE: mutated then claimed false positive",
        ],
        heads_after_attempt=[item_start_head, correction_head],
        dirty_after_attempt=[False, True],
        reset_fails=True,
    )
    runner.current_head = item_start_head

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "roll back" in str(caught.value).lower()
    assert len(runner.prompts) == 2
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == correction_head


@pytest.mark.unit
async def test_protocol_retry_non_fix_rolls_back_hosted_remote_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted protocol-retry rollback must rewind the published PR head, not just local."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    synced_head = "b" * 40
    state = MonitorState(last_push_sha=item_start_head)
    remote_rollbacks: list[dict[str, object]] = []

    async def _record_remote_rollback(*args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: duplicate of an earlier repaired item",
        ],
        heads_after_attempt=[synced_head, synced_head],
        dirty_after_attempt=[True, False],
    )
    runner._deps.adapter.is_hosted = True

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=item_start_head,
        state=state,
    )

    assert result.verdict == "false_positive"
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head
    assert state.last_push_sha == item_start_head
    assert not state.hosted_terminal_head_advanced
    assert len(remote_rollbacks) == 1
    assert remote_rollbacks[0]["rollback_target_sha"] == item_start_head
    assert remote_rollbacks[0]["expected_remote_head_sha"] == synced_head


@pytest.mark.unit
async def test_protocol_retry_non_fix_rolls_back_non_descendant_hosted_remote_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Amend/rebase sync updates last_push_sha without forward ancestry must still rollback."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    rewritten_head = "b" * 40
    state = MonitorState(last_push_sha=item_start_head)
    remote_rollbacks: list[dict[str, object]] = []

    async def _record_remote_rollback(*args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: duplicate of an earlier repaired item",
        ],
        heads_after_attempt=[rewritten_head, rewritten_head],
        dirty_after_attempt=[True, False],
    )
    runner._deps.adapter.is_hosted = True

    async def _sync_without_forward_ancestry(**kwargs: object) -> AgentRunResult:
        output = runner.outputs[runner.attempt]
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        synced_head = runner.heads_after_attempt[runner.attempt - 1]
        operation_start_head = str(kwargs.get("operation_start_head", ""))
        sync_state = kwargs.get("state")
        if (
            runner._deps.adapter.is_hosted
            and isinstance(sync_state, MonitorState)
            and synced_head.lower() != operation_start_head.lower()
        ):
            sync_state.last_push_sha = synced_head
            runner.current_head = synced_head
        return AgentRunResult(returncode=0, stdout=str(output), stderr="")

    runner._run_monitor_agent_with_service_recovery = _sync_without_forward_ancestry

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=item_start_head,
        state=state,
    )

    assert result.verdict == "false_positive"
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head
    assert state.last_push_sha == item_start_head
    assert not state.hosted_terminal_head_advanced
    assert len(remote_rollbacks) == 1
    assert remote_rollbacks[0]["rollback_target_sha"] == item_start_head
    assert remote_rollbacks[0]["expected_remote_head_sha"] == rewritten_head


@pytest.mark.unit
async def test_protocol_retry_non_fix_hosted_remote_rollback_failure_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed when hosted remote rollback cannot rewind the published PR head."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    synced_head = "b" * 40
    state = MonitorState(last_push_sha=item_start_head)

    async def _failed_remote_rollback(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _failed_remote_rollback,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: duplicate of an earlier repaired item",
        ],
        heads_after_attempt=[synced_head, synced_head],
        dirty_after_attempt=[True, False],
    )
    runner._deps.adapter.is_hosted = True

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=item_start_head,
            state=state,
        )

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_protocol_retry_non_fix_verdict_cleanup_failure_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40

    async def _failed_cleanup(**_kwargs: object) -> ValidationWorktreeCleanup:
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=ValidationWorktreeCheck(clean=False, untracked_paths=("leftover.txt",)),
            restore_ref=item_start_head,
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message="could not remove untracked files",
            cleanup_stderr="clean failed",
        )

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _failed_cleanup,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: duplicate of an earlier repaired item",
        ],
        heads_after_attempt=[fixed_head, fixed_head],
        dirty_after_attempt=[True, False],
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_protocol_retry_non_fix_verdict_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: duplicate of an earlier repaired item",
        ],
        heads_after_attempt=[fixed_head, fixed_head],
        dirty_after_attempt=[True, False],
        reset_fails=True,
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_fixed_rejected_when_only_same_directory_sibling_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6bdFvk: sibling-file edits must not satisfy inline FIXED."""
    reviewed_path = "src/awf/reviewed.py"
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: fixed the implementation in another module",
            "AWF-VERDICT: FIXED: still only the sibling module",
        ],
        heads_after_attempt=["b" * 40, "b" * 40],
        dirty_after_attempt=[True, True],
        path_touched=False,
    )
    thread = ReviewThread(
        thread_id="thread_cross_file",
        path=reviewed_path,
        line=42,
        body_excerpt="fix the helper used here",
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _address_thread(
            runner,
            workspace_id="ws_protocol",
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            thread=thread,
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head="a" * 40,
        )

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_fixed_rejected_when_same_file_unrelated_line_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue:5381831025: same-file edits away from the review line must not count."""
    reviewed_path = "src/awf/reviewed.py"
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: changed an unrelated line in the same file",
            "AWF-VERDICT: FIXED: still not at the review anchor",
        ],
        heads_after_attempt=["b" * 40, "b" * 40],
        dirty_after_attempt=[True, True],
        path_touched=True,
        line_touched=False,
    )
    thread = ReviewThread(
        thread_id="thread_same_file_other_line",
        path=reviewed_path,
        line=42,
        body_excerpt="fix the null check here",
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _address_thread(
            runner,
            workspace_id="ws_protocol",
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            thread=thread,
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head="a" * 40,
        )

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_bundled_inline_thread_rejects_outside_inline_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bundled inline threads still require path/line evidence for FIXED."""
    inline_path = "src/awf/common/github_client.py"
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: fixed review-body request in another module",
            "AWF-VERDICT: FIXED: still only outside inline path",
        ],
        heads_after_attempt=["b" * 40, "b" * 40],
        dirty_after_attempt=[True, True],
        path_touched=False,
    )
    thread = ReviewThread(
        thread_id="thread_bundle",
        path=inline_path,
        line=478,
        body_excerpt="inline anchor comment",
        review_context=ReviewComment(
            comment_id="R_bundle",
            body_excerpt="Fix comments.py instead",
            body="Fix something in comments.py instead",
        ),
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _address_thread(
            runner,
            workspace_id="ws_protocol",
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            thread=thread,
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head="a" * 40,
        )

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_fixed_rejected_when_contentful_descendant_is_unrelated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated README-only commits must not satisfy FIXED for a code review item."""
    reviewed_path = "src/target.py"
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: updated docs",
            "AWF-VERDICT: FIXED: still only docs",
        ],
        heads_after_attempt=["b" * 40, "b" * 40],
        dirty_after_attempt=[True, True],
        path_touched=False,
    )
    thread = ReviewThread(
        thread_id="thread_unrelated",
        path=reviewed_path,
        line=10,
        body_excerpt="fix the null check here",
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _address_thread(
            runner,
            workspace_id="ws_protocol",
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            thread=thread,
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head="a" * 40,
        )

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_operator_hint_keeps_no_code_fixed_exception(tmp_path: Path) -> None:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: replied on GitHub"],
        heads_after_attempt=["a" * 40],
    )

    result = await _invoke(runner, require_fix_evidence=False)

    assert result.verdict == "fix_committed"
    assert len(runner.prompts) == 1


@pytest.mark.unit
async def test_explicit_needs_human_is_not_reasked(tmp_path: Path) -> None:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: NEEDS_HUMAN: choose the public API contract"],
        heads_after_attempt=["a" * 40],
    )

    result = await _invoke(runner)

    assert result.verdict == "needs_human"
    assert result.reason == "choose the public API contract"
    assert len(runner.prompts) == 1


@pytest.mark.unit
async def test_provider_failure_after_protocol_retry_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed rollback after provider failure must abort instead of agent_failed."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing", _agent_error()],
        heads_after_attempt=[fixed_head, fixed_head],
        dirty_after_attempt=[True, False],
        reset_fails=True,
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 2
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_provider_failure_after_protocol_retry_rolls_back_unaccepted_commits(
    tmp_path: Path,
) -> None:
    """Provider failure must not publish first-attempt commits as agent_failed residue."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing", _agent_error()],
        heads_after_attempt=[fixed_head, fixed_head],
        dirty_after_attempt=[True, False],
    )

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == "AGENT_CLI_FAILED"
    assert len(runner.prompts) == 2
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_provider_failure_hosted_remote_rollback_failure_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed when hosted provider failure cannot rewind the published PR head."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    synced_head = "b" * 40
    state = MonitorState(last_push_sha=item_start_head)

    async def _failed_remote_rollback(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _failed_remote_rollback,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[synced_head],
    )
    runner._deps.adapter.is_hosted = True

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=item_start_head,
            state=state,
        )

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head
    assert state.last_push_sha == synced_head
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_provider_failure_clears_hosted_push_state_after_remote_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted provider failure must clear published-head state only after remote rewind."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    synced_head = "b" * 40
    state = MonitorState(last_push_sha=item_start_head)
    remote_rollbacks: list[dict[str, object]] = []

    async def _record_remote_rollback(*args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[synced_head],
    )
    runner._deps.adapter.is_hosted = True

    with pytest.raises(AgentVerdictExecutionError):
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=item_start_head,
            state=state,
        )

    assert state.last_push_sha == item_start_head
    assert not state.hosted_terminal_head_advanced
    assert runner.current_head == item_start_head
    assert len(remote_rollbacks) == 1
    assert remote_rollbacks[0]["rollback_target_sha"] == item_start_head
    assert remote_rollbacks[0]["expected_remote_head_sha"] == synced_head


@pytest.mark.unit
async def test_rollback_restores_last_push_sha_after_hosted_sync_advance(
    tmp_path: Path,
) -> None:
    """Hosted sync during provider failure must not leave last_push_sha advanced."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    synced_head = "b" * 40
    state = MonitorState(last_push_sha=item_start_head)
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[synced_head],
    )

    with pytest.raises(AgentVerdictExecutionError):
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=item_start_head,
            state=state,
        )

    assert state.last_push_sha == item_start_head
    assert not state.hosted_terminal_head_advanced
    assert runner.current_head == item_start_head
