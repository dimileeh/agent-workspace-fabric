"""A managed-key park code on the semantic pre-commit repair parks the workspace.

The semantic repair's ``AgentRunError`` handler normally retries the commit with
whatever the repair left behind and, if the hook still fails, replaces the
provider reason with ``POST_AGENT_COMMIT_PRECOMMIT_FAILED`` — terminally failing
the workspace. A managed-key budget/key stop must instead park it in the
``provider_budget`` block with the original reason and the worktree intact
(PRRT_kwDOSJAM6s614vKD).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import base as adapter_base
from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.provider_budget_park import (
    PROVIDER_BUDGET_BLOCK_TYPE,
    PROVIDER_BUDGET_RESUME_PHASE,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit._validation_git_probes import answer_validation_git_probes
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_001 import (
    _RecordingValidation,
    _seed_ready,
)
from tests.unit.control.test_executor_post_agent_commit_parts.test_executor_post_agent_commit_part_001 import (
    _make_executor,
    _precommit_ruff_check_and_format_output,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.mark.parametrize(
    "reason",
    [
        "PROVIDER_BUDGET_EXHAUSTED",
        "PROVIDER_ACCOUNT_CREDITS_EXHAUSTED",
        "PROVIDER_MANAGED_KEY_UNAVAILABLE",
        "PROVIDER_MANAGEMENT_KEY_MISSING",
    ],
)
async def test_semantic_repair_park_code_parks_before_commit_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    class _BudgetExhaustedOnRepairAdapter(adapter_base.AgentAdapter):
        runtime = AgentRuntime.codex

        @property
        def name(self) -> AgentRuntime:
            return AgentRuntime.codex

        def get_provider(self, model: str | None) -> str:
            return "openrouter"

        def _cli_args(self, *, model: str | None) -> list[str]:
            return []

        async def run(self, *, prompt: str, **kwargs: Any) -> adapter_base.AgentRunResult:
            del prompt
            if kwargs.get("log_source") != "post_agent_precommit_repair":
                return adapter_base.AgentRunResult(returncode=0, stdout="adapter ok", stderr="")
            raise adapter_base.AgentRunError(
                agent=self.name,
                result=CommandResult(returncode=1, stdout="", stderr="402 insufficient credits"),
                reason_code=reason,
                details={"retryable": False, "provider_budget": {"remaining_usd": "0.000000"}},
            )

    monkeypatch.setitem(adapter_base._REGISTRY, AgentRuntime.codex, _BudgetExhaustedOnRepairAdapter)
    fake = FakeCommandRunner()
    answer_validation_git_probes(fake)
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="fix_test.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("fix_test.py"),
    )  # semantic pre-commit failure -> agent repair
    validation = _RecordingValidation()
    ws_id = await _seed_ready(factory)

    await _make_executor(fake, factory, tmp_path, validation=validation).execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.blocked.value
    assert ws.block_type == PROVIDER_BUDGET_BLOCK_TYPE
    assert ws.block_reason_code == reason
    assert ws.block_resume_phase == PROVIDER_BUDGET_RESUME_PHASE
    assert ws.failure_reason is None
    # Parked before the commit retry: one commit attempt, never validated or pushed.
    assert len([call for call in fake.calls if "commit" in call.args]) == 1
    assert not [call for call in fake.calls if "push" in call.args]
    assert [phases for phases in validation.calls if phases != ("setup", "pre_agent")] == []


async def test_semantic_repair_park_code_from_superseded_executor_leaves_new_claim_running(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded executor's managed-key stop during repair cannot park (or
    fail) the replacement claimant's ``running`` row (PRRT_kwDOSJAM6s615vfG)."""

    class _ClaimLostThenBudgetExhaustedOnRepairAdapter(adapter_base.AgentAdapter):
        runtime = AgentRuntime.codex

        @property
        def name(self) -> AgentRuntime:
            return AgentRuntime.codex

        def get_provider(self, model: str | None) -> str:
            return "openrouter"

        def _cli_args(self, *, model: str | None) -> list[str]:
            return []

        async def run(self, *, prompt: str, **kwargs: Any) -> adapter_base.AgentRunResult:
            del prompt
            if kwargs.get("log_source") != "post_agent_precommit_repair":
                return adapter_base.AgentRunResult(returncode=0, stdout="adapter ok", stderr="")
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(ws_id)
                assert ws is not None
                ws.execution_claimed_by = "new-worker"
                await s.commit()
            raise adapter_base.AgentRunError(
                agent=self.name,
                result=CommandResult(returncode=1, stdout="", stderr="402 insufficient credits"),
                reason_code="PROVIDER_BUDGET_EXHAUSTED",
                details={"retryable": False},
            )

    monkeypatch.setitem(
        adapter_base._REGISTRY, AgentRuntime.codex, _ClaimLostThenBudgetExhaustedOnRepairAdapter
    )
    fake = FakeCommandRunner()
    answer_validation_git_probes(fake)
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="fix_test.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("fix_test.py"),
    )  # semantic pre-commit failure -> agent repair
    ws_id = await _seed_ready(factory)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ws.execution_claimed_by = "old-worker"
        await s.commit()

    await _make_executor(fake, factory, tmp_path, validation=_RecordingValidation()).execute(
        ws_id, execution_owner_id="old-worker"
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.running.value
    assert ws.execution_claimed_by == "new-worker"
    assert ws.block_type is None
    assert ws.failure_reason is None
