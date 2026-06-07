"""Regression tests for operator *directive* guidance hints in the PR monitor.

Split out of ``test_pr_monitor_operator_hints.py`` to keep that module under the
first-party file-size guardrail; these cover the issue #447 ``guide`` directive
path (directive carried distinctly from the audit ``reason``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressOperatorHint,
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    OperatorHint,
    PRStatus,
    decide,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _ready_status(
    *,
    head_sha: str = "abc1234567890def",
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


@pytest.mark.unit
def test_decide_returns_address_for_pending_directive_hint() -> None:
    hint = OperatorHint(
        reason="operator guidance recorded",
        directive="implement the forge-neutral fix, do not defer",
        operation_id="op_guide",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    action = decide(_ready_status(), state, MonitorConfig())

    assert action == AddressOperatorHint(hint=hint)


@pytest.mark.unit
async def test_operator_hint_cycle_prompt_uses_directive_over_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="operator guidance recorded",
        directive="implement the forge-neutral fix, do not defer",
        operation_id="op_guide",
        requested_at="2026-06-07T12:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    captured: dict[str, object] = {}

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _capture_prompt(**kwargs: object) -> VerdictResult:
        captured.update(kwargs)
        return VerdictResult(verdict="needs_human", reason="stop here")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _capture_prompt)

    await runner._run_operator_hint_cycle(
        workspace_id="ws_guide_directive",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_guide_directive",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    prompt = captured["prompt"]
    assert isinstance(prompt, str)
    assert "implement the forge-neutral fix, do not defer" in prompt
    assert "operator guidance recorded" not in prompt
    # The commit message must stay control-neutral: the same cycle handles both
    # remonitor and guide hints, so it must not hardcode "remonitor".
    assert captured["commit_message"] == "fix: address operator hint"
