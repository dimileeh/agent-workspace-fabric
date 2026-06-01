# PRRT_kwDOSJAM6s6GGZfH Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6GGZfH` reports that validation captures the
workspace `HEAD` before running the pre-validation worktree-clean guard. If
`HEAD` capture fails while the worktree is already dirty, AWF reports
`VALIDATION_INFRASTRUCTURE_ERROR` and never records the more actionable
`VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` guard failure.

Scope is limited to the validation loop in
`src/awf/control/executor/execution_validation.py` and focused regression
coverage for that failure precedence.

## Requirements Checklist

- Run `check_validation_worktree_clean` before treating missing workspace
  `HEAD` as an infrastructure failure.
- Preserve existing validation-run finalization behavior for dirty worktree and
  missing-HEAD guard failures.
- Add a regression test proving a dirty worktree is reported as
  `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` even when `HEAD` capture fails.
- Use focused validation only; full AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Add a targeted unit test in the existing executor validation edge tests.
2. Reorder the initial validation-loop preflight so the worktree-clean guard
   runs before the missing-HEAD failure branch.
3. Keep the already-started validation run semantics by starting a run before
   returning guard failures.
4. Run the focused test module or targeted node IDs that cover the changed
   paths.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q`

Pass criteria: the focused test file passes, including the new regression.
