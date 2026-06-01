"""Additional executor PR-monitor handoff setup coverage."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from tests.unit.control.test_executor_error_paths_parts import (
    test_executor_error_paths_part_013 as _part_013,
)

factory = _part_013.factory
fake = _part_013.fake
_make_executor = _part_013._make_executor
_RecordingValidation = _part_013._RecordingValidation
_seed_ready = _part_013._seed_ready


class TestExecutorMonitorHandoffSetupPart014:
    @pytest.mark.unit
    async def test_sync_feature_pr_monitor_factory_none_marks_unavailable_after_setup(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        validation = _RecordingValidation()
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_args, **_kwargs: None,
        )

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_message == (
                "adopted PR monitor handoff failed: no PR monitor configured"
            )
            assert ws.events[-1].reason_code == "PR_ADOPTION_MONITOR_UNAVAILABLE"
