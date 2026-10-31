"""The #1138 disposition refinements run on a normal poll, before ``decide()``.

Both the readopt ancestry check (D2) and the bot-acknowledgement agent check
(D1) run in the runner's per-poll steps, where no merge lock is held, so the
gate that ``decide()``'s action reaches re-audits with their results.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import runner as runner_module
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _StopAtDecideError(Exception):
    pass


async def test_both_steps_run_on_the_poll_before_decide(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    seen: dict[str, dict[str, Any]] = {}

    async def readopt(self: Any, **kwargs: Any) -> None:
        order.append("readopt")
        seen["readopt"] = kwargs

    async def ack(self: Any, **kwargs: Any) -> None:
        order.append("ack")
        seen["ack"] = kwargs

    def decide(*args: Any) -> Any:
        order.append("decide")
        raise _StopAtDecideError

    monkeypatch.setattr(runner_module, "verify_readopted_dispositions", readopt)
    monkeypatch.setattr(runner_module, "judge_pending_disposition_acks", ack)
    monkeypatch.setattr(runner_module, "decide", decide)
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload())
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(_StopAtDecideError):
        await runner.run(
            workspace_id=workspace_id, compose_project="proj", compose_file=tmp_path / "c.yml"
        )

    assert order == ["readopt", "ack", "decide"]
    assert seen["ack"]["compose_project"] == "proj"
    assert seen["ack"]["compose_file"] == tmp_path / "c.yml"
    assert seen["ack"]["workspace_id"] == seen["readopt"]["workspace_id"] == workspace_id
    assert seen["ack"]["state"] is seen["readopt"]["state"]
