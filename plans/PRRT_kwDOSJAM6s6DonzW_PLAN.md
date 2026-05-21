# PRRT_kwDOSJAM6s6DonzW Plan

## Problem Statement And Scope

The review thread reports that preserved active execution recovery treats every
`no_work` worktree classification as immediately replaceable. That is too broad
for a present, clean worktree whose `HEAD` is not ahead of `base_commit`: an
agent may still be running and simply not have committed yet. Scope is limited
to preserved active recovery behavior in `src/awf/control/worker.py` and focused
unit coverage in `tests/unit/control/test_worker.py`.

## Requirements Checklist

- Add a regression test proving a present clean worktree that is not ahead of
  base records salvage blocked during the preservation grace period instead of
  creating a replacement workspace.
- Preserve existing missing-worktree replacement behavior.
- After preservation grace expires, allow the no-local-commits recovery path to
  create the replacement as before.
- Keep the change scoped to the review-thread behavior.

## Implementation Steps

1. Add a focused unit test near the preserved active no-work tests.
2. Confirm the new test fails against the current unconditional `no_work`
   replacement path when practical.
3. Guard `clean_branch_not_ahead` `no_work` classifications with the existing
   salvage-blocked path until preservation grace expires.
4. Run the focused unit tests that cover the changed behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_clean_worktree_without_commits_waits_for_preservation_grace or preserved_active_without_usable_work_creates_one_replacement_with_lineage or preserved_active_without_usable_work_preserves_sync_remote_push_branch'`
  passes.
