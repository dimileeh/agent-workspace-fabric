"""Executor post-validation conformance recovery tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor import WorkspaceExecutor
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.runtime.planning import PLAN_CONFORMANCE_UNSATISFIED
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.control.test_executor_parts.test_executor_part_002 import (
    _adapter_prompts,
    _insert_validate_handoff_recovery_operation,
    _json_value,
    _queue_validation_head,
    _seed_ready_workspace,
)
from tests.unit.control.test_executor_parts.test_executor_part_002 import (
    executor as executor,  # noqa: F401 - pytest fixture imported for this shard
)
from tests.unit.control.test_executor_parts.test_executor_part_002 import (
    factory as factory,  # noqa: F401 - pytest fixture imported for this shard
)
from tests.unit.control.test_executor_parts.test_executor_part_002 import (
    fake as fake,  # noqa: F401 - pytest fixture imported for this shard
)


class TestExecutorPostValidationConformanceRecovery:
    @pytest.mark.unit
    async def test_post_validation_conformance_gap_stops_at_preserved_handoff_budget(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned-recovery",
                "planning": {
                    "required": True,
                    "plan_path": "docs/awf-plans/{workspace_id}.md",
                    "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
                    "max_iterations": 2,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )
        # Seed the worktree plan + (unsatisfied) conformance report the real
        # agent would write; the deposit must surface them even on the
        # preserved-FAILED stop path so the console stays uniform.
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "needs_iteration", "gaps": ["incomplete"]}',
            encoding="utf-8",
        )
        operation_id = "op_post_validation_conformance_gap"
        await _insert_validate_handoff_recovery_operation(
            factory,
            workspace_id=ws_id,
            operation_id=operation_id,
            requested_tier=1,
            conformance_overrides={"iteration": 1, "max_iterations": 2},
        )
        report_path = f"docs/awf-plans/{ws_id}.conformance.json"
        post_validation_gap_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Validation passed, but the API docs are still incomplete.",
                "gaps": ["Document the API endpoint required by the saved plan."],
            }
        )

        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=post_validation_gap_report)
        fake.queue_result(
            returncode=0,
            stdout=f"?? {report_path}\n",
        )
        fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD

        await executor.execute(ws_id)

        adapter_prompts = _adapter_prompts(fake)
        post_validation_conformance_prompts = [
            prompt
            for prompt in adapter_prompts
            if "Conformance phase" in prompt and "### Validation evidence" in prompt
        ]

        assert len(adapter_prompts) == 1
        assert post_validation_conformance_prompts == adapter_prompts
        assert [
            line
            for prompt in post_validation_conformance_prompts
            for line in prompt.splitlines()
            if line.startswith("Iteration: ")
        ] == ["Iteration: 2"]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            runs = (
                (
                    await s.execute(
                        text(
                            "SELECT status, reason_code FROM validation_runs "
                            "WHERE workspace_id = :workspace_id ORDER BY started_at"
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .all()
            )
            operation = (
                (
                    await s.execute(
                        text(
                            """
                            SELECT status, error_code, result, finished_at, payload,
                                   idempotency_key
                            FROM operations
                            WHERE id = :operation_id
                            """
                        ),
                        {"operation_id": operation_id},
                    )
                )
                .mappings()
                .one()
            )
            extra_validate_recovery_ops = (
                await s.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM operations
                        WHERE workspace_id = :workspace_id
                          AND type = 'validate'
                          AND status IN ('pending', 'running')
                          AND id <> :operation_id
                          AND idempotency_key LIKE 'pr_monitor:validate_only:%'
                        """
                    ),
                    {"workspace_id": ws_id, "operation_id": operation_id},
                )
            ).scalar_one()

        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "agent_failure"
        assert "Document the API endpoint required by the saved plan." in (ws.failure_message or "")
        assert [run["status"] for run in runs] == ["succeeded"]
        assert [run["reason_code"] for run in runs] == ["VALIDATION_OK"]
        assert operation["status"] == "failed"
        assert operation["error_code"] == PLAN_CONFORMANCE_UNSATISFIED
        assert operation["finished_at"] is not None
        payload = _json_value(operation["payload"])
        assert payload["owner"] == "pr_monitor"
        assert payload["source"] == "pr_monitor"
        assert payload["action"] == "validate_only"
        assert payload["requested_action"] == "validate"
        assert payload["requested_tier"] == 1
        assert payload["source_head_sha"] == "deadbeef01"
        assert payload["source_base_sha"] == "a" * 40
        assert payload["target_branch"] == "development"
        assert payload["remote_branch"] == f"awf/{ws_id}"
        assert payload["recovery_mode"] == "validate_only"
        assert payload["conformance"]["iteration"] == 1
        assert payload["conformance"]["max_iterations"] == 2
        assert operation["idempotency_key"].startswith("pr_monitor:validate_only:")
        result = _json_value(operation["result"])
        assert result["reason_code"] == PLAN_CONFORMANCE_UNSATISFIED
        assert result["requested_tier"] == 1
        assert extra_validate_recovery_ops == 0

        # Preserved FAILED workspace still surfaces its plan + (unsatisfied)
        # conformance report in the served artifact dir.
        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").is_file()
        assert (served_dir / "conformance.json").read_text(
            encoding="utf-8"
        ) == '{"status": "needs_iteration", "gaps": ["incomplete"]}'
