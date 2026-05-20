# PRRT_kwDOSJAM6s6De8qX Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6De8qX` reports that worker-restart recovery claims can now lease workspaces in `validating` or `pushing`, while `WorkspaceExecutor.execute()` immediately requires the workspace to be `running`. That can create repeated no-op recovery attempts after the claim is taken.

Scope is limited to the executor/repository worker-restart recovery claim path and its unit coverage. Do not change branch management, push behavior, or unrelated runtime recovery policy.

## Requirements Checklist

- Worker-restart execution recovery claims must only return workspaces executable by `WorkspaceExecutor.execute()`.
- Workspaces already in `validating` or `pushing` must not receive an execution lease through `_claim_ready()`.
- Existing `running` worker-restart recovery lease behavior must continue to support unset, stale, or same-owner claims and reject another live owner.
- Add or update regression coverage before implementing the production change.
- Commit only the files changed for this thread.

## Implementation Steps

1. Update the existing unit test that covers `validating` and `pushing` worker-restart recovery claims so it expects no claim and no execution lease mutation.
2. Run the targeted test to confirm it fails against the current implementation.
3. Restrict the repository-level worker-restart recovery execution claim statuses to `running`.
4. Run the targeted executor tests for worker-restart recovery claim behavior.
5. Run a narrow lint/typecheck or broader unit command if needed by touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py -k worker_restart_recovery -q`
  - Passes with the updated regression.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py tests/unit/control/test_executor.py`
  - Passes with no lint errors.
