"""Fail-closed FIXED evidence gate and per-item isolation regressions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_task_tag_threading import (
    _MonitorAgentServiceRecoveryRunner,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _evidence_runner(
    *,
    stdout: str,
    dirty: bool,
    heads: list[str | None] | None = None,
    returncode: int = 0,
    head_descends: bool | None = None,
) -> SimpleNamespace:
    """Stub runner for ``_invoke_cli_for_verdict_result`` evidence checks."""
    head_iter = iter(heads or [])

    async def _suppress(_workspace_id: str) -> bool:
        return False

    async def _adapter_run(**_kwargs: object) -> AgentRunResult:
        if returncode != 0:
            from awf.adapters.base import AgentRunError
            from awf.db.enums import AgentRuntime

            raise AgentRunError(
                agent=AgentRuntime.claude_code,
                result=CommandResult(returncode=returncode, stdout=stdout, stderr="fail"),
                reason_code="AGENT_CLI_FAILED",
            )
        return AgentRunResult(returncode=0, stdout=stdout, stderr="")

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return dirty

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        try:
            return next(head_iter)
        except StopIteration:
            return heads[-1] if heads else None

    async def _head_descends_from(
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        del worktree_path
        if head_descends is not None:
            return head_descends
        return ancestor.lower() != descendant.lower()

    async def _handle_provider_agent_run_error(
        _workspace_id: str,
        _exc: object,
        *,
        state: object | None = None,
    ) -> None:
        del state

    return _MonitorAgentServiceRecoveryRunner(
        _worktrees_root=Path("/tmp"),
        _provider_recovery_suppresses_cli=_suppress,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _rev_parse_head=_rev_parse_head,
        _head_descends_from=_head_descends_from,
        _handle_provider_agent_run_error=_handle_provider_agent_run_error,
        _deps=SimpleNamespace(adapter=SimpleNamespace(run=_adapter_run)),
    )


@pytest.mark.unit
async def test_fixed_claim_with_dirty_commit_is_fix_committed() -> None:
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: renamed helper",
        dirty=True,
        heads=["b" * 40],
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_fixed_dirty",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head="a" * 40,
    )

    assert result.verdict == "fix_committed"
    assert result.reason == "renamed helper"


@pytest.mark.unit
async def test_explicit_fixed_without_head_advance_stays_unresolved() -> None:
    start = "a" * 40
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed but no commit",
        dirty=False,
        heads=[start],
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_fixed_no_change",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_operator_hint_fixed_without_head_advance_is_accepted() -> None:
    """Operator hints may complete GitHub-side work without a commit."""
    start = "a" * 40
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: replied on GitHub only",
        dirty=False,
        heads=[start],
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_operator_hint_no_code",
        prompt="p",
        commit_message="fix: address operator hint",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
        require_fix_evidence=False,
    )

    assert result.verdict == "fix_committed"
    assert result.reason == "replied on GitHub only"


@pytest.mark.unit
async def test_operator_hint_fixed_without_evidence_still_agent_failed_on_cli_error() -> None:
    start = "a" * 40
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: claimed during crash",
        dirty=False,
        heads=[start],
        returncode=1,
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_operator_hint_cli_fail",
        prompt="p",
        commit_message="fix: address operator hint",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
        require_fix_evidence=False,
    )

    assert result.verdict == "agent_failed"


@pytest.mark.unit
async def test_fixed_claim_with_forward_head_advance_is_fix_committed(
    tmp_path: Path,
) -> None:
    start = "a" * 40
    end = "b" * 40
    workspace_id = "ws_fixed_forward"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: agent committed locally",
        dirty=False,
        heads=[end],
        head_descends=True,
    )
    runner._worktrees_root = tmp_path

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
    )

    assert result.verdict == "fix_committed"
    assert result.reason == "agent committed locally"


@pytest.mark.unit
async def test_fixed_claim_with_backward_head_move_stays_unresolved(
    tmp_path: Path,
) -> None:
    start = "b" * 40
    older = "a" * 40
    workspace_id = "ws_fixed_backward"
    (tmp_path / workspace_id).mkdir()
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: reset to older tip",
        dirty=False,
        heads=[older],
        head_descends=False,
    )
    runner._worktrees_root = tmp_path

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id=workspace_id,
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_markerless_output_never_upgraded_by_dirty_commit() -> None:
    runner = _evidence_runner(
        stdout="Committed a fix without a marker",
        dirty=True,
        heads=["b" * 40],
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_markerless_dirty",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head="a" * 40,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "unrecognized_or_markerless_verdict"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "expected_verdict", "expected_reason"),
    [
        (
            "AWF-VERDICT: FALSE POSITIVE: reviewer misread the guard",
            "false_positive",
            "reviewer misread the guard",
        ),
        (
            "AWF-VERDICT: DEFER: track follow-up outside this PR",
            "defer",
            "track follow-up outside this PR",
        ),
        (
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must choose the policy",
            "needs_human",
            "maintainer must choose the policy",
        ),
    ],
)
async def test_explicit_non_fix_verdicts_survive_dirty_or_hosted_advance(
    stdout: str,
    expected_verdict: str,
    expected_reason: str,
) -> None:
    start = "a" * 40
    runner = _evidence_runner(stdout=stdout, dirty=True, heads=["b" * 40])
    state = MonitorState()

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_retain_explicit",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == expected_verdict
    assert result.reason == expected_reason


@pytest.mark.unit
async def test_hosted_fixed_requires_terminal_head_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = "a" * 40
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: hosted repair committed",
        dirty=False,
        heads=[start],
    )
    state = MonitorState(last_push_sha=start)

    async def _run_with_hosted_advance(**kwargs: object) -> AgentRunResult:
        state_arg = kwargs.get("state")
        assert isinstance(state_arg, MonitorState)
        state_arg.hosted_terminal_head_advanced = True
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted repair committed",
            stderr="",
        )

    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", _run_with_hosted_advance
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_hosted_fixed",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == "fix_committed"
    assert result.reason == "hosted repair committed"


@pytest.mark.unit
async def test_hosted_fixed_without_head_advance_stays_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = "a" * 40
    runner = _evidence_runner(
        stdout="AWF-VERDICT: FIXED: hosted no advance",
        dirty=False,
        heads=[start],
    )
    state = MonitorState(last_push_sha=start)

    async def _run_without_advance(**kwargs: object) -> AgentRunResult:
        state_arg = kwargs.get("state")
        assert isinstance(state_arg, MonitorState)
        state_arg.hosted_terminal_head_advanced = False
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted no advance",
            stderr="",
        )

    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _run_without_advance)

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_hosted_no_advance",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "fixed_without_head_advance"


@pytest.mark.unit
async def test_hosted_advance_does_not_accept_markerless_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = "a" * 40
    runner = _evidence_runner(
        stdout="plain hosted reply with no marker",
        dirty=False,
        heads=["b" * 40],
    )
    state = MonitorState(last_push_sha=start)

    async def _run_markerless_with_advance(**kwargs: object) -> AgentRunResult:
        state_arg = kwargs.get("state")
        assert isinstance(state_arg, MonitorState)
        state_arg.hosted_terminal_head_advanced = True
        return AgentRunResult(
            returncode=0,
            stdout="plain hosted reply with no marker",
            stderr="",
        )

    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", _run_markerless_with_advance
    )

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_hosted_markerless",
        prompt="p",
        commit_message="fix: x",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=start,
    )

    assert result.verdict == "needs_human"
    assert result.reason == "unrecognized_or_markerless_verdict"


@pytest.mark.unit
async def test_another_thread_commit_does_not_leak_into_later_item_evidence() -> None:
    """Item 2 start head is post-item-1; no-op FIXED must not inherit item-1 evidence."""
    item1_start = "a" * 40
    item1_end = "b" * 40
    item2_start = item1_end

    first = await comments._invoke_cli_for_verdict_result(
        _evidence_runner(
            stdout="AWF-VERDICT: FIXED: first thread",
            dirty=True,
            heads=[item1_end],
        ),
        workspace_id="ws_leak_1",
        prompt="p",
        commit_message="fix: 1",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=item1_start,
    )
    assert first.verdict == "fix_committed"

    second = await comments._invoke_cli_for_verdict_result(
        _evidence_runner(
            stdout="AWF-VERDICT: FIXED: second thread no change",
            dirty=False,
            heads=[item2_start],
        ),
        workspace_id="ws_leak_2",
        prompt="p",
        commit_message="fix: 2",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        operation_start_head=item2_start,
    )
    assert second.verdict == "needs_human"
    assert second.reason == "fixed_without_head_advance"


class _ResolveCaptureClient:
    def __init__(self, runner: FakeCommandRunner) -> None:
        self._runner = runner
        self.resolved: list[str] = []

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
        retry: bool = True,
    ) -> PRStatus:
        del repo, pr_number, base_behind_count, retry
        return PRStatus(
            number=42,
            head_sha="abc1234567890def",
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        )

    async def resolve_thread(self, *, thread_id: str) -> None:
        self.resolved.append(thread_id)


@pytest.mark.unit
async def test_fix_cycle_two_item_burst_only_evidenced_fixed_resolves(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First item FIXED+evidence resolves; second markerless stays unresolved."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="newsha\n")  # rev-parse HEAD after push
    gh = _ResolveCaptureClient(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)

    async def _address_thread(**kwargs: object) -> str:
        thread = kwargs["thread"]
        assert isinstance(thread, ReviewThread)
        if thread.thread_id == "T_first":
            return "fix_committed"
        return "needs_human"

    async def _protected_scope_push_block(**_kwargs: object) -> None:
        return None

    async def _validated_git_push_result(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_scope_push_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_git_push_result)

    state = MonitorState()
    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(
            ReviewThread(
                thread_id="T_first",
                path="src/a.py",
                line=1,
                body_excerpt="fix first",
                author="reviewer",
            ),
            ReviewThread(
                thread_id="T_second",
                path="src/b.py",
                line=2,
                body_excerpt="fix second",
                author="reviewer",
            ),
        ),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert state.threads_addressed_ids.get("T_first") == "fix_committed"
    assert state.threads_addressed_ids.get("T_second") == "needs_human"
    assert gh.resolved == ["T_first"]
