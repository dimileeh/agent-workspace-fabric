# PRRT_kwDOSJAM6s6DxuzC Open PR Lookup Failure Plan

## Problem Statement And Scope

The review thread reports that a failed preserved-branch open PR lookup is
treated as equivalent to no open PR after the preservation grace expires. That
can let no-work salvage create a replacement workspace even though the lookup
may have failed because of transient GitHub CLI, auth, or network issues and an
open PR may still exist. The scope is limited to preserved active execution
recovery in `ControlWorker` and the unit coverage around pushed-branch PR
lookup failures.

## Requirements Checklist

- Preserve automatic replacement when the open PR lookup succeeds and confirms
  there is no open PR for a clean branch with no local work.
- Treat a failed open PR lookup as an operator-recoverable ambiguity after
  grace expires, including for clean no-work branches and committed branches
  that would otherwise be validated and pushed.
- Preserve the during-grace behavior where lookup failures record
  `ACTIVE_EXECUTION_SALVAGE_BLOCKED` and retry later.
- Keep failed lookup payload evidence on the recovery event.

## Implementation Steps

1. Update the committed-work validation and no-work replacement regressions to
   cover successful empty open PR lookups instead of lookup failures.
2. Add or update focused regressions proving expired failed lookups with clean
   committed and no-work branches record operator-required recovery and do not
   create validation or replacement work.
3. Refactor `_recover_preserved_active_execution` so failed branch lookup
   remains ambiguous after grace and cannot fall into no-PR replacement or stale
   cleanup paths.
4. Run the focused regression before the fix when practical, then rerun the
   focused preserved-active lookup tests after implementation.

## Verification Commands And Pass Criteria

- The failed-lookup/no-work regression fails before the worker fix and passes
  after it.
- A narrow preserved-active lookup subset passes:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_retries_during_grace tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_no_open_pr_validates_committed_work tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_committed_work_requires_operator_recovery tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_missing_lineage_keeps_committed_work_operator_recoverable tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_no_open_pr_with_no_local_work_replaces tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_no_local_work_requires_operator_recovery -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passes.
