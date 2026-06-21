# PR614 Remaining CI Failures Plan

## Scope

Fix the remaining focused failures from PR #614's current failed CI run after
the shard 8 line-limit split:

- `TestExecProcessCleanupSafety.test_agent_cleanup_failure_fails_infrastructure_before_validation`
- `test_execute_ci_repair_missing_operation_start_head_is_terminal`
- `test_execute_comment_repair_missing_operation_start_head_is_terminal`

## Diagnosis

- Executor cleanup failure records `EXEC_PROCESS_CLEANUP_FAILED`, then missing
  HEAD recovery appends `GIT_OBJECT_MISSING`, making the cleanup failure no
  longer the terminal event.
- Monitor CI/comment repair start HEAD capture receives an empty PR head SHA,
  falls back to the open merge candidate, and proceeds to invoke the repair
  agent instead of failing closed with `REPAIR_START_HEAD_UNAVAILABLE`.

## Implementation

1. Make agent cleanup failure recovery stop after the cleanup infrastructure
   failure has been recorded when the worktree HEAD is missing.
2. Keep the generic start-head helper's candidate fallback available for
   callers that explicitly rely on it, but let CI/comment repair callers require
   the PR status head as their only fallback baseline.
3. Run only focused tests and targeted lint/type checks for touched files.

## Validation

Record focused command results in
`plans/PR614_REMAINING_CI_FAILURES_VALIDATION.md`. Full AWF/GitHub validation is
owned by AWF after agent completion.
