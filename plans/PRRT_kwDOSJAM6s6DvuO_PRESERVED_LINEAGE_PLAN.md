# PRRT_kwDOSJAM6s6DvuO Preserved Lineage Plan

## Problem Statement And Scope

An unresolved PR review thread reports that an expired failed preserved-branch PR
lookup can still be short-circuited by missing `task_attempt` lineage before the
worker classifies the preserved worktree. The scope is limited to
`ControlWorker` preserved active execution recovery and its regression coverage.

## Requirements Checklist

- Add a regression test for expired failed branch lookup plus missing task
  attempt lineage when the preserved worktree contains committed work.
- Preserve the existing during-grace behavior: transient branch lookup failures
  and missing lineage should keep recording `SALVAGE_BLOCKED`.
- Do not request automatic validation without a concrete `attempt_id` and
  `task_id`.
- After grace, classify the worktree before declaring the salvage unrecoverable
  for the failed-branch-lookup path.
- If classification finds committed work but lineage is missing, record
  `OPERATOR_REQUIRED` with both the committed-work classification and branch
  lookup failure payload.

## Implementation Steps

1. Add a focused unit test in `tests/unit/control/test_worker.py` that creates a
   preserved pushing workspace with no task attempt, a committed worktree, and a
   failing branch PR lookup.
2. Confirm the new regression fails against the current implementation.
3. Refactor `_recover_preserved_active_execution` in
   `src/awf/control/worker.py` so the expired failed-lookup path can reach
   worktree classification before handling missing lineage.
4. Route committed work with missing lineage to operator-required recovery rather
   than stale failure.
5. Run the focused test and narrow surrounding preserved-active recovery tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::<test node> -q`
  fails before the fix and passes after it.
- A narrow preserved-active subset in `tests/unit/control/test_worker.py` passes
  after the fix.
