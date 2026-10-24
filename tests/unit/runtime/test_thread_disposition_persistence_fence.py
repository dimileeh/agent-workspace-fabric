"""Per-thread writes obey the same claim fence as full monitor persistence."""

from types import SimpleNamespace

import pytest

from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.thread_disposition_reply import _persist_item_keys
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace


@pytest.mark.parametrize(
    ("owner", "claimed_by", "suppressed", "allowed"),
    [
        ("old", "new", False, False),
        (None, "new", False, False),
        ("same", "same", True, False),
        (None, None, False, True),
        ("same", "same", False, True),
    ],
)
async def test_per_thread_write_respects_current_monitor_claim(
    owner, claimed_by, suppressed, allowed
):
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await seed_monitoring_workspace(factory)
        original = {"thread": "fixed", "evidence": "successor", "unrelated": "keep"}
        async with factory.begin() as session:
            row = await WorkspaceRepository(session).get_for_update(workspace_id)
            assert row is not None
            row.monitor_claimed_by = claimed_by
            row.monitor_threads_addressed = original
        state = MonitorState()
        state.monitor_writes_suppressed = suppressed
        state.threads_addressed_ids = {"thread": "needs_human"}
        runner = SimpleNamespace(
            _monitor_owner_id=owner, _deps=SimpleNamespace(session_factory=factory)
        )

        await _persist_item_keys(
            runner,
            workspace_id=workspace_id,
            state=state,
            item_id="thread",
            keys=("thread", "evidence"),
        )

        async with factory() as session:
            row = await WorkspaceRepository(session).get(workspace_id)
            assert row is not None
            assert row.monitor_threads_addressed == (
                {"thread": "needs_human", "unrelated": "keep"} if allowed else original
            )
        assert state.monitor_writes_suppressed is (not allowed)
