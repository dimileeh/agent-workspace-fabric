"""Executor PR-monitor handoff stale-status coverage. (split part)"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    _seed_ready,
    factory,
    fake,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_017 import (
    _PR_ADOPTION_POLICY,
    _OkSetupValidation,
)

_IMPORTED_FIXTURES = (factory, fake)


class TestSyncFeaturePrHandoffStaleAfterMonitorBuiltSplit:
    @pytest.mark.unit
    async def test_feature_handoff_stale_status_after_monitor_built_skips(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Feature handoff skips monitor.run when status changes after monitor build."""
        # Mirror of the release case for the adopted-feature-PR handoff: the
        # monitor builds, but the workspace leaves ``running`` before adoption is
        # persisted, so the handoff records a stale-action skip and stops.
        validation = _OkSetupValidation()
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy=_PR_ADOPTION_POLICY,
        )

        class _Monitor:
            async def run(self, *, workspace_id: str, **_kwargs: Any) -> None:
                """Fail fast if stale-status fencing did not skip monitor execution."""
                raise AssertionError("monitor must not run after a stale-status skip")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_a, **_k: _Monitor(),
        )

        original_build = executor._build_handoff_pr_monitor

        async def _build_handoff_pr_monitor(**kwargs: Any) -> Any:
            """Cancel the workspace after monitor build to simulate a stale-status race."""
            monitor = await original_build(**kwargs)
            async with factory() as s:
                repo = WorkspaceRepository(s)
                ws = await repo.get(ws_id)
                assert ws is not None
                await repo.transition(
                    ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCELLED"
                )
                await s.commit()
            return monitor

        monkeypatch.setattr(executor, "_build_handoff_pr_monitor", _build_handoff_pr_monitor)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].payload["action"] == "sync_feature_pr_handoff"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
