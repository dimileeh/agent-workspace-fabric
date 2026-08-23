"""Bounded correction-retry and item-scoped FIXED evidence regressions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult
from awf.common.github_client import RepoRef
from awf.db.enums import AgentRuntime
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_monitor import MonitorState, ReviewComment, ReviewThread
from awf.runtime.pr_monitor_runner import comment_verdict, comments
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictExecutionError,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from awf.runtime.pr_monitor_runner.constants import _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
)


class _VerdictRunner(SimpleNamespace):
    def __init__(
        self,
        *,
        worktrees_root: Path,
        outputs: list[str | AgentRunError],
        heads_after_attempt: list[str],
        dirty_after_attempt: list[bool] | None = None,
        path_touched: bool = True,
        in_item_scope: bool = True,
        provider_error_action: BaseException | None = None,
        provider_recovery_suppress_attempts: frozenset[int] | None = None,
        reset_fails: bool = False,
        rev_parse_sequence: list[str | None] | None = None,
    ) -> None:
        super().__init__()
        self._worktrees_root = worktrees_root
        self.outputs = outputs
        self.heads_after_attempt = heads_after_attempt
        self.dirty_after_attempt = dirty_after_attempt or [False] * len(outputs)
        self.path_touched = path_touched
        self.in_item_scope = in_item_scope
        self.provider_error_action = provider_error_action
        self.provider_recovery_suppress_attempts = provider_recovery_suppress_attempts
        self.reset_fails = reset_fails
        self.rev_parse_sequence = rev_parse_sequence
        self.rev_parse_index = 0
        self._workspace_runtime_context = ""
        self.prompts: list[str] = []
        self.attempt = 0
        self.current_head = heads_after_attempt[0]
        self.reset_targets: list[str] = []
        self.provider_recovery_check_count = 0
        self._deps = SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=SimpleNamespace(run=self._run_git),
        )

    async def _run_git(self, cmd: list[str]) -> CommandResult:
        if "reset" in cmd and "--hard" in cmd:
            self.reset_targets.append(cmd[-1])
            if self.reset_fails:
                return CommandResult(returncode=1, stdout="", stderr="reset failed")
            self.current_head = cmd[-1]
        return CommandResult(returncode=0, stdout="", stderr="")

    async def _provider_recovery_suppresses_cli(self, _workspace_id: str) -> bool:
        attempt = self.provider_recovery_check_count
        self.provider_recovery_check_count += 1
        return (
            self.provider_recovery_suppress_attempts is not None
            and attempt in self.provider_recovery_suppress_attempts
        )

    async def _run_monitor_agent_with_service_recovery(self, **kwargs: object) -> AgentRunResult:
        self.prompts.append(str(kwargs["prompt"]))
        output = self.outputs[self.attempt]
        self.attempt += 1
        if isinstance(output, AgentRunError):
            state = kwargs.get("state")
            synced_head = self.heads_after_attempt[self.attempt - 1]
            if (
                isinstance(state, MonitorState)
                and synced_head.lower() != str(kwargs.get("operation_start_head", "")).lower()
            ):
                state.last_push_sha = synced_head
                state.hosted_terminal_head_advanced = True
                self.current_head = synced_head
            raise output
        state = kwargs.get("state")
        synced_head = self.heads_after_attempt[self.attempt - 1]
        operation_start_head = str(kwargs.get("operation_start_head", ""))
        if (
            self._deps.adapter.is_hosted
            and isinstance(state, MonitorState)
            and synced_head.lower() != operation_start_head.lower()
        ):
            state.last_push_sha = synced_head
            state.hosted_terminal_head_advanced = True
            self.current_head = synced_head
        return AgentRunResult(returncode=0, stdout=output, stderr="")

    async def _commit_dirty_worktree(self, **_kwargs: object) -> bool:
        index = self.attempt - 1
        self.current_head = self.heads_after_attempt[index]
        return self.dirty_after_attempt[index]

    async def _rev_parse_head(self, _worktree_path: Path) -> str | None:
        if self.rev_parse_sequence is not None:
            if self.rev_parse_index >= len(self.rev_parse_sequence):
                return self.current_head
            value = self.rev_parse_sequence[self.rev_parse_index]
            self.rev_parse_index += 1
            if value is not None:
                self.current_head = value
            return value
        return self.current_head

    async def _head_descends_from(
        self,
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        del worktree_path
        return ancestor != descendant

    async def _commit_trees_differ(
        self,
        *,
        worktree_path: Path,
        left: str,
        right: str,
    ) -> bool:
        del worktree_path
        return left != right

    async def _commit_range_touches_path(self, **_kwargs: object) -> bool:
        return self.path_touched

    async def _commit_range_in_item_scope(self, **_kwargs: object) -> bool:
        return self.in_item_scope

    async def _resolve_task_tag(self, _workspace_id: str) -> str | None:
        return None

    async def _hosted_pr_identity_for_workspace(
        self,
        _workspace_id: str,
        *,
        state: MonitorState | None = None,
    ) -> dict[str, object]:
        del state
        return {
            "head_repo_url": "https://example.invalid/awf.git",
            "head_ref": "awf/ws_protocol",
            "repo_url": "https://example.invalid/awf.git",
        }

    async def _invoke_cli_for_verdict_result(
        self, **kwargs: object
    ) -> comment_verdict.VerdictResult:
        return await comment_verdict._invoke_cli_for_verdict_result(self, **kwargs)  # type: ignore[arg-type]

    async def _handle_provider_agent_run_error(
        self,
        _workspace_id: str,
        _exc: AgentRunError,
        *,
        state: object | None = None,
    ) -> None:
        del state
        if self.provider_error_action is not None:
            raise self.provider_error_action


@pytest.fixture(autouse=True)
def _safe_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)


def _agent_error(stdout: str = "") -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=1, stdout=stdout, stderr="provider failed"),
        reason_code="AGENT_CLI_FAILED",
    )


async def _invoke(
    runner: _VerdictRunner,
    *,
    require_fix_evidence: bool = True,
):
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head="a" * 40,
        require_fix_evidence=require_fix_evidence,
    )


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
async def test_bundled_review_body_fix_accepts_outside_inline_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review-body-only fixes outside thread.path count for bundled review items."""
    inline_path = "src/awf/common/github_client.py"
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: fixed review-body request in another module"],
        heads_after_attempt=["b" * 40],
        dirty_after_attempt=[True],
        in_item_scope=False,
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

    verdict = await _address_thread(
        runner,
        workspace_id="ws_protocol",
        repo=RepoRef(owner="o", name="r"),
        pr_number=1,
        thread=thread,
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head="a" * 40,
    )

    assert verdict == "fix_committed"
    assert len(runner.prompts) == 1


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


@pytest.mark.unit
async def test_provider_failure_cleans_dirty_worktree_when_head_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uncommitted agent edits must be discarded when provider failure leaves HEAD unchanged."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
    item_start_head = "a" * 40
    cleanup_calls: list[dict[str, object]] = []

    async def _cleanup(**kwargs: object) -> ValidationWorktreeCleanup:
        cleanup_calls.append(kwargs)
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=False, paths=("dirty.py",)),
            restore_ref=item_start_head,
            cleaned_paths=("dirty.py",),
        )

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[item_start_head],
    )

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == "AGENT_CLI_FAILED"
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0]["restore_ref"] == item_start_head
    assert cleanup_calls[0]["worktree_path"] == worktree
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_provider_recovery_after_agent_run_rolls_back_unaccepted_commits(
    tmp_path: Path,
) -> None:
    """In-run provider recovery must roll back agent edits before retrying."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_provider_recovery_after_agent_run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise ProviderRecoveryRetryError()

    runner._run_monitor_agent_with_service_recovery = _raise_provider_recovery_after_agent_run

    with pytest.raises(ProviderRecoveryRetryError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_provider_recovery_after_agent_run_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed in-run provider-recovery rollback must abort instead of retrying."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_provider_recovery_after_agent_run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise ProviderRecoveryRetryError()

    runner._run_monitor_agent_with_service_recovery = _raise_provider_recovery_after_agent_run

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _MonitorAgentServiceRecoveryFailedError("agent service unhealthy"),
        lambda: _MonitorAgentServiceRecoverySupersededError("monitor claim lost"),
    ],
)
async def test_service_recovery_exit_after_agent_run_rolls_back_unaccepted_commits(
    tmp_path: Path,
    exc_factory: object,
) -> None:
    """Post-invocation service-recovery exits must roll back agent edits before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    service_recovery_exc = exc_factory()  # type: ignore[operator]

    async def _raise_service_recovery_after_agent_run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise service_recovery_exc

    runner._run_monitor_agent_with_service_recovery = _raise_service_recovery_after_agent_run

    with pytest.raises(type(service_recovery_exc)):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_unexpected_failure_after_agent_run_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed rollback after an unexpected invocation error must fail closed."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_unexpected_after_agent_run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise RuntimeError("unexpected failure after agent edit")

    runner._run_monitor_agent_with_service_recovery = _raise_unexpected_after_agent_run

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "roll back" in str(caught.value).lower()
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_unexpected_failure_rolls_back_before_post_exception_hook_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback must precede post-exception hook repair so repair failure cannot strand edits."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    mirror_path.mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    hook_repair_stages: list[str] = []

    monkeypatch.setattr(
        comment_verdict,
        "mirror_path_for_worktree",
        lambda _path: mirror_path,
    )

    async def _repair_mirror_hooks_path(_path: Path) -> bool:
        stage = (
            "before_comment_agent" if not hook_repair_stages else "after_comment_agent_exception"
        )
        hook_repair_stages.append(stage)
        if stage == "after_comment_agent_exception":
            raise OSError("hooks poisoned")
        return True

    monkeypatch.setattr(comment_verdict, "repair_mirror_hooks_path", _repair_mirror_hooks_path)

    async def _raise_unexpected_after_agent_run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise RuntimeError("unexpected failure after agent edit")

    runner._run_monitor_agent_with_service_recovery = _raise_unexpected_after_agent_run

    with pytest.raises(_MonitorMirrorHooksPathRepairFailedError):
        await _invoke(runner)

    assert hook_repair_stages == [
        "before_comment_agent",
        "after_comment_agent_exception",
    ]
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_service_recovery_exit_after_agent_run_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed service-recovery rollback must abort instead of propagating the exit."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_service_recovery_failed_after_agent_run(
        **kwargs: object,
    ) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise _MonitorAgentServiceRecoveryFailedError("agent service unhealthy")

    runner._run_monitor_agent_with_service_recovery = _raise_service_recovery_failed_after_agent_run

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        ),
        lambda: _MonitorHeadObjectMissingError(
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            "missing head",
        ),
        lambda: _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"),
    ],
)
async def test_infrastructure_service_recovery_exit_rollback_failure_preserves_reason(
    tmp_path: Path,
    exc_factory: object,
) -> None:
    """Failed rollback must not mask infrastructure service-recovery exit reason codes."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )
    service_recovery_exc = exc_factory()  # type: ignore[operator]

    async def _raise_infrastructure_exit_after_agent_run(
        **kwargs: object,
    ) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise service_recovery_exc

    runner._run_monitor_agent_with_service_recovery = _raise_infrastructure_exit_after_agent_run

    with pytest.raises(type(service_recovery_exc)) as caught:
        await _invoke(runner)

    assert caught.value is service_recovery_exc
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_provider_recovery_before_protocol_correction_rolls_back_first_attempt_commit(
    tmp_path: Path,
) -> None:
    """Provider recovery on the correction attempt must not strand first-attempt commits."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        provider_recovery_suppress_attempts=frozenset({1}),
    )

    with pytest.raises(ProviderRecoveryRetryError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_non_fixed_verdict_rejected_when_rollback_cannot_resolve_head(
    tmp_path: Path,
) -> None:
    """Unreadable HEAD during rollback must fail closed before accepting a verdict."""
    (tmp_path / "ws_protocol").mkdir()
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing", "AWF-VERDICT: NEEDS_HUMAN: design choice"],
        heads_after_attempt=[fixed_head, fixed_head],
        dirty_after_attempt=[True, False],
        rev_parse_sequence=[fixed_head, fixed_head, None],
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "roll back" in str(caught.value).lower()
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_rollback_fails_closed_when_head_unreadable(tmp_path: Path) -> None:
    """Direct rollback must reject unreadable HEAD instead of reporting success."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=["a" * 40],
        rev_parse_sequence=[None],
    )

    ok = await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_protocol",
        worktree_path=worktree,
        item_start_head="a" * 40,
        state=None,
    )

    assert ok is False


@pytest.mark.unit
async def test_provider_recovery_before_protocol_correction_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed rollback before provider recovery must abort instead of retrying."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        provider_recovery_suppress_attempts=frozenset({1}),
        reset_fails=True,
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_provider_error_does_not_consume_protocol_retry(tmp_path: Path) -> None:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=["a" * 40],
        provider_error_action=ProviderRecoveryRetryError(),
    )

    with pytest.raises(ProviderRecoveryRetryError):
        await _invoke(runner)

    assert runner.prompts == ["ORIGINAL REVIEW PROMPT"]


@pytest.mark.unit
async def test_worker_cancellation_after_agent_edit_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Worker cancel must roll back agent edits before propagating CancelledError."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_cancel_after_agent_edit(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise asyncio.CancelledError()

    runner._run_monitor_agent_with_service_recovery = _raise_cancel_after_agent_edit

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_worker_cancellation_during_provider_recovery_check_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Worker cancel during pre-launch provider recovery check must roll back first."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_cancel_on_correction_pre_launch(_workspace_id: str) -> bool:
        if runner.provider_recovery_check_count == 1:
            raise asyncio.CancelledError()
        return await _VerdictRunner._provider_recovery_suppresses_cli(runner, _workspace_id)

    runner._provider_recovery_suppresses_cli = _raise_cancel_on_correction_pre_launch

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_policy_blocked_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Supply-chain policy block during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_policy_blocked_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked review fix.")

    runner._commit_dirty_worktree = _raise_policy_blocked_during_commit

    with pytest.raises(_MonitorPolicyBlockedError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_protected_scope_diff_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Protected-scope diff failure during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    diff_exc = ProtectedScopeDiffError("protected-scope diff unavailable")

    async def _raise_protected_scope_diff_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise diff_exc

    runner._commit_dirty_worktree = _raise_protected_scope_diff_during_commit

    with pytest.raises(ProtectedScopeDiffError) as caught:
        await _invoke(runner)

    assert caught.value is diff_exc
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_provider_recovery_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Provider recovery during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_provider_recovery_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise ProviderRecoveryRetryError()

    runner._commit_dirty_worktree = _raise_provider_recovery_during_commit

    with pytest.raises(ProviderRecoveryRetryError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _MonitorAgentServiceRecoveryFailedError("agent service unhealthy"),
        lambda: _MonitorAgentServiceRecoverySupersededError("monitor claim lost"),
    ],
)
async def test_service_recovery_exit_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
    exc_factory: object,
) -> None:
    """Post-invocation service-recovery exits during commit sink must roll back first."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    service_recovery_exc = exc_factory()  # type: ignore[operator]

    async def _raise_service_recovery_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise service_recovery_exc

    runner._commit_dirty_worktree = _raise_service_recovery_during_commit

    with pytest.raises(type(service_recovery_exc)):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        ),
        lambda: _MonitorHeadObjectMissingError(
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            "missing head",
        ),
        lambda: _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"),
    ],
)
async def test_infrastructure_exit_during_commit_sink_rollback_failure_preserves_reason(
    tmp_path: Path,
    exc_factory: object,
) -> None:
    """Failed commit-sink rollback must not mask terminal infrastructure reason codes."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )
    infrastructure_exc = exc_factory()  # type: ignore[operator]

    async def _raise_infrastructure_exit_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise infrastructure_exc

    runner._commit_dirty_worktree = _raise_infrastructure_exit_during_commit

    with pytest.raises(type(infrastructure_exc)) as caught:
        await _invoke(runner)

    assert caught.value is infrastructure_exc
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_provider_recovery_during_commit_sink_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed commit-sink provider-recovery rollback must abort instead of retrying."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_provider_recovery_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise ProviderRecoveryRetryError()

    runner._commit_dirty_worktree = _raise_provider_recovery_during_commit

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_worker_cancellation_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Worker cancel during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_cancel_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise asyncio.CancelledError()

    runner._commit_dirty_worktree = _raise_cancel_during_commit

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_worker_cancellation_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Worker cancel must fail closed when rollback cannot discard agent edits."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_cancel_after_agent_edit(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise asyncio.CancelledError()

    runner._run_monitor_agent_with_service_recovery = _raise_cancel_after_agent_edit

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head
