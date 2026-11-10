"""A managed-key park code on the initial agent run parks before salvage.

The initial ``AgentRunError`` handler normally salvages uncommitted work and
continues; if HEAD advanced, the run proceeds to validation and PR creation
without reaching the ``_mark_failed`` park sink. A managed-key budget/key
failure must instead stop the unfinished task in the ``provider_budget`` block
with its worktree intact (PRRT_kwDOSJAM6s613nGw).
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
async def test_initial_agent_park_code_parks_before_salvage_validation_and_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    class _BudgetExhaustedAdapter(adapter_base.AgentAdapter):
        runtime = AgentRuntime.codex

        @property
        def name(self) -> AgentRuntime:
            return AgentRuntime.codex

        def get_provider(self, model: str | None) -> str:
            return "openrouter"

        def _cli_args(self, *, model: str | None) -> list[str]:
            return []

        async def run(self, *, prompt: str, **kwargs: Any) -> adapter_base.AgentRunResult:
            del prompt, kwargs
            raise adapter_base.AgentRunError(
                agent=self.name,
                result=CommandResult(returncode=1, stdout="", stderr="402 insufficient credits"),
                reason_code=reason,
                details={"retryable": False, "provider_budget": {"remaining_usd": "0.000000"}},
            )

    monkeypatch.setitem(adapter_base._REGISTRY, AgentRuntime.codex, _BudgetExhaustedAdapter)
    fake = FakeCommandRunner()
    answer_validation_git_probes(fake)
    # Partial edits the salvage path would commit and advance HEAD with.
    fake.respond_when(lambda args: "rev-list" in args, stdout="1\n")
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
    # Stopped before salvage: nothing staged or committed, never validated or pushed.
    assert not [call for call in fake.calls if "add" in call.args and "-A" in call.args]
    assert not [call for call in fake.calls if "commit" in call.args]
    assert not [call for call in fake.calls if "push" in call.args]
    assert [phases for phases in validation.calls if phases != ("setup", "pre_agent")] == []
