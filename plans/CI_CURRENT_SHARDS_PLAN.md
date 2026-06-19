# CI current shards plan

## Problem statement and scope

PR #614 has failing or unstable GitHub Actions CI on the AWF-managed branch.
The most recent completed failure was on an older commit and failed Python
coverage shards, while the current HEAD run is still pending. Scope is limited
to diagnosing the current shard failures, fixing only real code or test defects
that reproduce on the current branch, and avoiding protected workflow or broad
validation changes.

## Requirements checklist

- [ ] Preserve AWF branch ownership: do not switch branches, push, rebase, or
  run broad AWF/GitHub-owned validation locally.
- [ ] Inspect current PR check status and use failed shard logs when available.
- [ ] Use focused repro commands for observed failures before editing.
- [ ] Keep changes scoped to the failing behavior; do not edit protected
  workflows or weaken checks.
- [ ] Add or adjust meaningful focused tests for any behavior change.
- [ ] Run focused verification for touched files and failing repros only.
- [ ] Record validation evidence in `plans/CI_CURRENT_SHARDS_VALIDATION.md`.
- [ ] Commit the local fix with a conventional commit message.

## Implementation steps

1. Inspect PR #614 Actions runs and identify failed jobs for current HEAD.
2. Run focused local repros for current shard failures or for stale failures
   that still reproduce on the current branch.
3. If a repro fails, inspect the smallest affected production/test surface.
4. Implement the narrowest fix and keep test assertions behavioral.
5. Re-run the focused repros plus narrow lint/type checks for edited files.
6. Write validation notes and commit the scoped change locally.

## Verification commands and pass criteria

- Current PR check/log inspection identifies the actionable failed job or shows
  that no current failed job is available yet.
- Focused repro commands for the observed failures pass after the fix.
- Focused lint/type checks for touched Python files pass where applicable.
- Full AWF/GitHub validation and coverage gates are left to AWF after agent
  completion, per the workspace contract.
