# PRRT_kwDOSJAM6s6Dckhk Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6Dckhk` reports that preserved-active branch
open-PR lookup failures are currently returned as `None`, which the recovery
caller can treat the same as no open PR. For a restarted pushing workspace with
a clean/not-ahead local worktree, that can create a replacement workspace even
though an open PR may already exist remotely.

Scope is limited to preserved-active execution recovery in
`src/awf/control/worker.py` and focused worker unit coverage.

## Requirements Checklist

- Distinguish branch open-PR lookup failures from a successful lookup with no
  PR matches.
- Keep the existing committed-work fallback: if local work is clean and ahead,
  resolver failure can still request validation salvage.
- Do not create a replacement workspace when lookup failed and the local
  worktree is clean/not-ahead.
- Record an operator-recoverable salvage event for the failed-lookup +
  clean/not-ahead case, including non-secret lookup failure context.
- Preserve existing behavior for genuine no-match lookup results.

## Implementation Steps

1. Add a failing unit regression for a preserved pushing workspace where branch
   PR lookup raises and the local worktree is clean/not-ahead.
2. Change `_resolve_preserved_active_branch_open_pr` so resolver exceptions
   return a distinct lookup-failed result with safe payload metadata.
3. Update preserved-active recovery to treat lookup-failed + no-work
   classification as operator-required instead of replacement creation, while
   leaving committed-work validation fallback intact.
4. Run the focused new regression and adjacent preserved-active branch lookup
   tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_no_local_work_is_operator_recoverable -q`
  passes after first failing before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_no_local_work_is_operator_recoverable tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_falls_back_to_worktree_salvage tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_without_usable_work_creates_one_replacement_with_lineage tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_ambiguity_is_operator_recoverable -q`
  passes.
