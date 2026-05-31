# PR329 Merge Conflict Plan

## Scope

Resolve the `origin/development` merge conflict in
`src/awf/runtime/pr_monitor_runner/helpers.py` without switching branches or
pushing.

## Steps

1. Inspect the conflicted helper and both merge sides.
2. Preserve the base refactor that extracts non-check reviewer settle logic to
   `reviewer_settle.py`.
3. Preserve the PR-side remonitor freeze behavior by moving the missing logic
   into the extracted `reviewer_settle.py` module.
4. Remove conflict markers from `helpers.py` and keep compatibility aliases for
   existing imports.
5. Run focused checks for the touched helper/module behavior only.
6. Record validation evidence and commit the merge resolution locally.
