"""Bounded correction-retry and item-scoped FIXED evidence regressions."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult
from awf.common.github_client import RepoRef
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import ReviewComment, ReviewThread
from awf.runtime.pr_monitor_runner import comment_verdict, comments
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError


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
        reset_fails: bool = False,
    ) -> None:
        super().__init__()
        self._worktrees_root = worktrees_root
        self.outputs = outputs
        self.heads_after_attempt = heads_after_attempt
        self.dirty_after_attempt = dirty_after_attempt or [False] * len(outputs)
        self.path_touched = path_touched
        self.in_item_scope = in_item_scope
        self.provider_error_action = provider_error_action
        self.reset_fails = reset_fails
        self._workspace_runtime_context = ""
        self.prompts: list[str] = []
        self.attempt = 0
        self.current_head = heads_after_attempt[0]
        self.reset_targets: list[str] = []
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
        return False

    async def _run_monitor_agent_with_service_recovery(self, **kwargs: object) -> AgentRunResult:
        self.prompts.append(str(kwargs["prompt"]))
        output = self.outputs[self.attempt]
        self.attempt += 1
        if isinstance(output, AgentRunError):
            raise output
        return AgentRunResult(returncode=0, stdout=output, stderr="")

    async def _commit_dirty_worktree(self, **_kwargs: object) -> bool:
        index = self.attempt - 1
        self.current_head = self.heads_after_attempt[index]
        return self.dirty_after_attempt[index]

    async def _rev_parse_head(self, _worktree_path: Path) -> str:
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
async def test_fixed_accepted_when_contentful_descendant_touches_related_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread.path on one file may be fixed by a commit in the same directory."""
    reviewed_path = "src/awf/reviewed.py"
    operation_start_head = "a" * 40
    fixed_head = "b" * 40
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: fixed the implementation in another module"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        path_touched=False,
        in_item_scope=True,
    )
    thread = ReviewThread(
        thread_id="thread_cross_file",
        path=reviewed_path,
        line=42,
        body_excerpt="fix the helper used here",
    )

    verdict = await _address_thread(
        runner,
        workspace_id="ws_protocol",
        repo=RepoRef(owner="o", name="r"),
        pr_number=1,
        thread=thread,
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=operation_start_head,
    )

    assert verdict == "fix_committed"
    assert len(runner.prompts) == 1
    assert not await runner._commit_range_touches_path(
        worktree_path=worktree,
        left=operation_start_head,
        right=fixed_head,
        path=reviewed_path,
    )
    params = inspect.signature(comment_verdict._invoke_cli_for_verdict_result).parameters
    assert "evidence_item_path" in params


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
        in_item_scope=False,
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
