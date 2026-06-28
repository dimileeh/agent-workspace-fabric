"""Executor recovery validation provenance tests split from part 004."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor import WorkspaceExecutor
from tests.unit.control.test_executor_parts.test_executor_part_004 import (
    _json_value,
    _queue_pre_push_diagnostics,
    _queue_validation_head,
    _seed_ready_workspace,
)
from tests.unit.control.test_executor_parts.test_executor_part_004 import (
    executor as executor,  # noqa: F401 - pytest fixture imported for this shard
)
from tests.unit.control.test_executor_parts.test_executor_part_004 import (
    factory as factory,  # noqa: F401 - pytest fixture imported for this shard
)
from tests.unit.control.test_executor_parts.test_executor_part_004 import (
    fake as fake,  # noqa: F401 - pytest fixture imported for this shard
)


class TestExecutorRecoveryValidationProvenance:
    @pytest.mark.unit
    async def test_recovery_validation_records_required_tier_and_finishes_operation(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory, test_commands=["ruff check ."])
        async with factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE workspaces
                    SET task_class = 'refactor_task'
                    WHERE id = :workspace_id
                    """
                ),
                {"workspace_id": ws_id},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO operations (
                        id,
                        workspace_id,
                        type,
                        status,
                        payload,
                        created_at
                    )
                    VALUES (
                        'op_validate_recovery',
                        :workspace_id,
                        'validate',
                        'pending',
                        '{"reason":"validation_insufficient_tier"}',
                        :created_at
                    )
                    """
                ),
                {"workspace_id": ws_id, "created_at": datetime.now(UTC)},
            )
            await session.commit()

        fake.queue_result(returncode=0, stdout="codex finished")  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="ruff ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1\n")

        await executor.execute(ws_id)

        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT tier, status
                        FROM validation_runs
                        WHERE workspace_id = :workspace_id
                        """
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .all()
            )
            operations = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT status, payload, result, finished_at
                        FROM operations
                        WHERE id = 'op_validate_recovery'
                        """
                        )
                    )
                )
                .mappings()
                .one()
            )

        assert rows == [{"tier": 2, "status": "succeeded"}]
        assert operations["status"] == "succeeded"
        assert operations["finished_at"] is not None
        assert _json_value(operations["payload"])["requested_tier"] == 2
        assert _json_value(operations["result"])["requested_tier"] == 2
