"""Bounded correction-retry and item-scoped FIXED evidence regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError


class _VerdictRunner(SimpleNamespace):
    def __init__(
        self,
        *,
        worktrees_root: Path,
        outputs: list[str | AgentRunError],
        heads_after_attempt: list[str],
        dirty_after_attempt: list[bool] | None = None,
        provider_error_action: BaseException | None = None,
    ) -> None:
        super().__init__()
        self._worktrees_root = worktrees_root
        self.outputs = outputs
        self.heads_after_attempt = heads_after_attempt
        self.dirty_after_attempt = dirty_after_attempt or [False] * len(outputs)
        self.provider_error_action = provider_error_action
        self.prompts: list[str] = []
        self.attempt = 0
        self.current_head = heads_after_attempt[0]
        self._deps = SimpleNamespace(adapter=SimpleNamespace(is_hosted=False))

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


@pytest.mark.unit
async def test_fixed_accepted_when_contentful_descendant_touches_different_file(
    tmp_path: Path,
) -> None:
    """A review attached to one file may require a fix in another file."""
    (tmp_path / "ws_protocol").mkdir()
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: fixed the implementation in another module"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    result = await _invoke(runner)

    assert result.verdict == "fix_committed"
    assert result.reason == "fixed the implementation in another module"
    assert len(runner.prompts) == 1


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
