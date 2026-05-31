# Comment Repair Workflow Scope Failure Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F7CNj` reports that comment-repair pushes rejected by GitHub for missing `workflow` scope can re-enter `AddressComments` repeatedly instead of surfacing required operator action. The scope is limited to PR monitor comment-repair failure handling; no workflow or protected configuration files are changed.

## Requirements Checklist

- Add a focused regression proving `AddressComments` treats `GITHUB_WORKFLOW_SCOPE_REQUIRED` as terminal operator action.
- Preserve existing fix-cycle state rollback/requeue behavior for workflow-scope failures.
- Align comment-repair push failure handling with existing sync-base and CI-repair workflow-scope termination behavior.
- Use focused validation only; full AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add a unit test for the `AddressComments` branch that stubs `_run_fix_cycle` to return a failed `_GitPushResult` with reason `GITHUB_WORKFLOW_SCOPE_REQUIRED`.
2. Confirm the new test fails before implementation because `_terminate_failed` is not called and the action returns non-terminal.
3. Update `src/awf/runtime/pr_monitor_runner/loop.py` so comment repair terminates on `push_result.workflow_scope_required`.
4. Re-run the focused regression and relevant focused lint/test checks.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::<new test> -q`
  - Before implementation: fails for the missing terminal handling.
  - After implementation: passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Passes for the focused workflow-scope regression file.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/loop.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
  - Passes with no lint errors.
