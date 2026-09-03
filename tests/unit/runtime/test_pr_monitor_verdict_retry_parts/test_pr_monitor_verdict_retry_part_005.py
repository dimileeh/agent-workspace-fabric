"""Bounded correction-retry rollback / FIXED evidence regressions (part 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import MonitorState, ReviewComment, ReviewThread
from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_rollback, comments
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_NON_FIXED_WITH_MUTATION,
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

    assert caught.value.reason_code == AGENT_NON_FIXED_WITH_MUTATION
    assert "roll back" in str(caught.value).lower()
    assert len(runner.prompts) == 2
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == correction_head


@pytest.mark.unit
async def test_mutation_classification_persistent_head_probe_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Persistent HEAD-probe failure during mutation rollback must stay typed.

    Production regression for PRRT_kwDOSJAM6s6exBWQ: when a correction is
    classified as mutated and the rollback helper's initial ``_rev_parse_head``
    raises (e.g. OSError while spawning Git), the raw exception escaped before
    ``rollback_ok`` was assigned. ``fix_cycle`` could not handle it as
    ``AgentVerdictProtocolError``, so ``AGENT_NON_FIXED_WITH_MUTATION`` was lost
    and unaccepted edits remained.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    correction_head = "c" * 40
    # Sequence: attempt0 start, attempt0 evidence, post-attempt0 tip,
    # correction start, pre-sink HEAD, correction evidence, mutation-gate end,
    # mutation rollback HEAD probe (raises and stays raising).
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: claimed without evidence",
            "AWF-VERDICT: FALSE POSITIVE: mutated then claimed false positive",
        ],
        heads_after_attempt=[item_start_head, correction_head],
        dirty_after_attempt=[False, True],
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_persistently_on_mutation_rollback(
        _worktree_path: Path,
    ) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls <= 7:
            return runner.current_head
        raise OSError("git spawn failed during mutation rollback rev-parse")

    runner._rev_parse_head = _raise_persistently_on_mutation_rollback

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_NON_FIXED_WITH_MUTATION
    assert "roll back" in str(caught.value).lower()
    assert len(runner.prompts) == 2
    assert rev_parse_calls >= 8
    assert runner.reset_targets == []
    assert runner.current_head == correction_head


@pytest.mark.unit
async def test_mutation_classification_rollback_preserves_reason_coded_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reason-coded rollback failures on mutation path must not collapse.

    Mirrors the correction-end / post-attempt tip guards: typed reason-coded
    exceptions from rollback dependencies must propagate unchanged when mutation
    classification attempts rollback (PRRT_kwDOSJAM6s6exBWQ).
    """
    from awf.runtime.pr_monitor_runner.types import (
        _MonitorAgentServiceRecoveryFailedError,
    )

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
    )
    runner.current_head = item_start_head

    async def _raise_reason_coded_rollback(
        _runner: object = None,
        **_kwargs: object,
    ) -> bool:
        raise _MonitorAgentServiceRecoveryFailedError(
            "hosted rollback dependency failed",
            reason_code="AGENT_SERVICE_RECOVERY_FAILED",
        )

    monkeypatch.setattr(
        comment_verdict_rollback,
        "_rollback_unaccepted_protocol_retry_changes",
        _raise_reason_coded_rollback,
    )

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == "AGENT_SERVICE_RECOVERY_FAILED"
    assert len(runner.prompts) == 2
    assert runner.reset_targets == []


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
async def test_protocol_retry_rollback_rewinds_hosted_remote_despite_git_config_restore_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e0xSU: config restore failure must not skip hosted remote rewind.

    Local reset/cleanup finish before ``restore_item_start_local_git_configs``.
    If that restore fails closed, rollback must still rewind the published
    remote head and clear ``last_push_sha``; overall success remains False.
    """
    from types import SimpleNamespace

    from awf.common.commands import CommandResult

    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    item_start_head = "a" * 40
    published_head = "b" * 40
    state = MonitorState(last_push_sha=published_head)
    state.hosted_terminal_head_advanced = True
    remote_rollbacks: list[dict[str, object]] = []

    async def _record_remote_rollback(*_args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    async def _cleanup(**_kwargs: object) -> ValidationWorktreeCleanup:
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=item_start_head,
        )

    async def _rev_parse_head(_path: Path) -> str:
        return published_head

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "rev-parse" in cmd:
            return CommandResult(returncode=0, stdout=f"{published_head}\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    async def _hosted_identity(_workspace_id: str, *, state: object = None) -> object:
        del _workspace_id, state
        return SimpleNamespace(owner="o", repo="r", number=1)

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint."
        "restore_item_start_local_git_configs",
        lambda _path: False,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=True),
            runner=SimpleNamespace(run=_run),
        ),
        _rev_parse_head=_rev_parse_head,
        _hosted_pr_identity_for_workspace=_hosted_identity,
    )

    ok = await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_protocol",
        worktree_path=worktree,
        item_start_head=item_start_head,
        item_start_last_push_sha=item_start_head,
        state=state,
    )

    assert ok is False
    assert len(remote_rollbacks) == 1
    assert remote_rollbacks[0]["rollback_target_sha"] == item_start_head
    assert remote_rollbacks[0]["expected_remote_head_sha"] == published_head
    assert state.last_push_sha == item_start_head
    assert state.hosted_terminal_head_advanced is False


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
async def test_provider_failure_persistent_head_probe_failure_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent HEAD-probe failure after provider error must stay typed.

    Production regression for review 5098116384: when the provider raises
    ``AgentRunError`` after changing the worktree and the rollback helper's
    initial ``_rev_parse_head`` also raises (e.g. OSError while spawning Git),
    the unguarded await replaced the provider failure with a raw exception
    before ``rollback_ok`` was assigned. The intended provider reason and
    rollback-failure event were lost, and unaccepted edits could remain.

    This scenario only invokes protocol-retry rollback once — on the
    ``AgentRunError`` path after the correction attempt — so patching the
    helper exercises that site without colliding with attempt-0 tip probes.
    """
    (tmp_path / "ws_protocol").mkdir()
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing", _agent_error()],
        heads_after_attempt=[fixed_head, fixed_head],
        dirty_after_attempt=[True, False],
    )

    async def _raise_oserror_on_rollback(
        _runner: object = None,
        **_kwargs: object,
    ) -> bool:
        raise OSError("git spawn failed during provider-failure rollback rev-parse")

    monkeypatch.setattr(
        comment_verdict_rollback,
        "_rollback_unaccepted_protocol_retry_changes",
        _raise_oserror_on_rollback,
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "roll back" in str(caught.value).lower()
    assert "provider failure" in str(caught.value).lower()
    assert len(runner.prompts) == 2
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_provider_failure_rollback_preserves_reason_coded_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reason-coded provider-failure rollback failures must not collapse.

    Mirrors the mutation / non-FIXED-accept / correction-end guards for review
    5098116384: typed reason-coded exceptions from rollback dependencies must
    propagate unchanged when cleaning unaccepted edits after ``AgentRunError``.
    """
    from awf.runtime.pr_monitor_runner.types import (
        _MonitorAgentServiceRecoveryFailedError,
    )

    (tmp_path / "ws_protocol").mkdir()
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing", _agent_error()],
        heads_after_attempt=[fixed_head, fixed_head],
        dirty_after_attempt=[True, False],
    )

    async def _raise_reason_coded_rollback(
        _runner: object = None,
        **_kwargs: object,
    ) -> bool:
        raise _MonitorAgentServiceRecoveryFailedError(
            "hosted rollback dependency failed",
            reason_code="AGENT_SERVICE_RECOVERY_FAILED",
        )

    monkeypatch.setattr(
        comment_verdict_rollback,
        "_rollback_unaccepted_protocol_retry_changes",
        _raise_reason_coded_rollback,
    )

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == "AGENT_SERVICE_RECOVERY_FAILED"
    assert len(runner.prompts) == 2
    assert runner.reset_targets == []


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
