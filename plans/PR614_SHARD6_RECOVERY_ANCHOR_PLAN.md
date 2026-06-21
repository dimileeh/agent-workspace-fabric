# PR614 Shard 6 Recovery Anchor Plan

## Problem Statement And Scope

PR #614 current-head CI also fails in `python-coverage-shards (6)`. The
failure cluster shows missing-HEAD recovery using a stale `operation_start_head`
where tests expect fallback to the merge candidate head after the original
anchor is unavailable.

Scope is limited to the missing-HEAD recovery anchor behavior exercised by the
failing PR monitor runner tests. CI workflow and unrelated monitor behavior are
out of scope.

## Requirements Checklist

- Reproduce representative shard 6 failures with focused pytest commands.
- Identify where `_commit_dirty_worktree`/remote repair selects the recovery
  anchor for missing HEAD object repair.
- Prefer a verified/fallback merge candidate head when the operation-start head
  is stale or unavailable.
- Preserve reason-coded failure behavior and existing protected-scope repair
  checks.
- Run focused pytest for the failing shard 6 tests touched by the fix.
- Do not run full AWF/GitHub-owned broad validation locally.

## Implementation Steps

1. Inspect the failing tests and current remote repair helpers.
2. Run a narrow repro for the representative stale-anchor failure.
3. Implement the smallest production/test fixture fix required by the observed
   root cause.
4. Re-run the representative failing tests and narrow lint/type checks for any
   touched files.

## Verification Commands And Pass Criteria

- Focused pytest for representative failing tests from shard 6 passes.
- Narrow Ruff check for touched files passes.

Full AWF/GitHub validation remains managed by AWF after agent completion.
