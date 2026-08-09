"""Regression coverage for terminal failures during a needs-human re-ask."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    (
        _MonitorAgentRuntimeOwnershipRepairFailedError("ownership repair failed"),
        _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING_UNRECOVERABLE"),
        _MonitorMirrorHooksPathRepairFailedError(),
        _MonitorPolicyBlockedError("policy blocked"),
    ),
)
async def test_needs_human_reason_reask_reraises_terminal_repair_errors(
    error: Exception,
) -> None:
    """Terminal repair failures must reach the fix-cycle reason-code handlers."""

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise error

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        pytest.fail("terminal re-ask error must not be replaced with a missing reason")

    runner = SimpleNamespace(
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )

    with pytest.raises(type(error)) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id="ws_1",
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch="awf/ws_1",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value is error
