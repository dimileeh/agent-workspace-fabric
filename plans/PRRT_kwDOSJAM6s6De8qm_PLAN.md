# PRRT_kwDOSJAM6s6De8qm Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6De8qm` reports that preserved active execution
recovery looks up an open PR by head branch while also filtering by the
workspace's original base branch. If the PR still exists but was retargeted, the
lookup can miss it and send recovery down replacement or operator-required
paths instead of reattaching the PR monitor.

Scope is limited to the preserved active branch PR recovery lookup in
`ControlWorker` and its focused unit coverage. Do not change PR creation,
monitor merge policy, branch management, or unrelated recovery behavior.

## Requirements Checklist

- Preserved active branch PR recovery must search open PRs by recovered head
  branch without filtering by `workspace.branch_base`.
- Matched, failed, ambiguous, and fallback-to-branch-name recovery behavior must
  preserve existing safety handling.
- Existing head repository mismatch and multiple-match ambiguity protections
  must remain in place.
- Add or update regression coverage before implementing the production change.
- Commit only files changed for this review thread.

## Implementation Steps

1. Update focused preserved active branch PR recovery tests to expect the
   resolver call to omit the base branch filter.
2. Run a targeted test before the production change to confirm the new
   expectation fails against the current implementation.
3. Change preserved active recovery to pass `base_branch=None` for branch-based
   open PR recovery.
4. Run the targeted preserved active branch PR recovery tests.
5. Run a narrow lint check for touched Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_pushed_branch" -q`
  - Passes with the updated regression coverage.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passes with no lint errors.
