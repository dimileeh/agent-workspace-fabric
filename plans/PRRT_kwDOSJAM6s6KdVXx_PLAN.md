# PRRT_kwDOSJAM6s6KdVXx plan — do not own the entire live staged index

## Problem statement

Review thread `PRRT_kwDOSJAM6s6KdVXx` (PR #615,
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py:579`) reports that the
pre-push dirty finalize ownership gate unions the *staged* delta
`git diff --name-status -z --cached operation_start_head`. Per `git diff -h`,
the `--cached [<commit>]` form compares the **current index** against the
commit. An unrelated file staged after the repair-start dirty guard but before
pre-push validation therefore appears in `owned_delta_paths` exactly as if the
operation had staged it. The gate then passes, `_commit_dirty_worktree` runs a
fresh `git status` and `git add -A --` on every non-ignored dirty path (so the
unrelated staged file is committed), and the post-commit re-validation
(`PRRT_kwDOSJAM6s6KZP8f`/`Ka0aO`) does not catch it because the pre-commit
`owned_delta_paths` already held that path. The unrelated staged file is silently
swept into the PR instead of failing closed.

This is the same class of over-broadening defect already fixed for the live
working-tree delta (`PRRT_kwDOSJAM6s6KbbE6`) and for the untracked fold-in
(`PRRT_kwDOSJAM6s6KcSj`): a *live* diff/fold at finalization time treats every
current index/untracked state as operation-owned merely because the repair-start
guard proved the tree clean at `operation_start_head`. The reviewer's explicit
ask is to base ownership on "paths captured by the repair operation rather than
whatever is in the live index at finalization time."

## Scope

- The operation's *committed* delta (`operation_start_head..HEAD`) is the only
  set the operation actually captured: a successful finalize commit moves the
  operation's owned residue into the committed delta, and the committed delta is
  the post-commit re-validation baseline. It is the correct ownership proxy.
- The staged delta (`--cached operation_start_head`) is the live index, not a
  captured set. The gate cannot distinguish "staged by the operation" (the
  `KYd-r` case: `_commit_dirty_worktree` returned False before committing
  because `git commit` failed after a successful `git add -A`) from "staged by
  an unrelated process after the repair-start guard." Both appear identically in
  `--cached operation_start_head`. This is the same dilemma `bbE6` resolved for
  the working-tree delta and `cSj` resolved for the untracked fold-in.
- The minimal correct fix that honors the reviewer's request is to **remove the
  staged delta branch** from `_operation_owned_delta_paths`, mirroring the
  `bbE6`/`cSj` fixes. The `KYd-r` recovery (operation-owned staged dirt from a
  failed commit) regresses to fail-closed `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`
  — visible and human-recoverable, not a silent sweep. Restoring it without the
  over-broadening requires capturing the operation's attempted paths (the
  `stage_paths` the commit sink computes) and threading them to the gate — a
  larger change tracked as a deferred follow-up (same defer shape as `bbE6`'s
  `KaUHP` defer and `cSj`'s `Ka0aK` defer).
- No new abstractions, no unrelated refactor, no protected-file edits, no caller
  signature changes.

## Requirements checklist

- [ ] Add a regression test (TDD red): a tracked file staged after the
      repair-start guard by an unrelated process (present in the staged delta
      but NOT committed) is NOT swept into the PR — the finalize skips and the
      push fails closed as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`. Currently
      the staged-delta branch treats it as owned and commits it (the bug).
- [ ] Remove the staged delta branch from `_operation_owned_delta_paths` (drop
      the `git diff --name-status -z --cached operation_start_head` diff, its
      `monitor.pre_push_dirty_finalize_staged_delta_unavailable` warning, and
      the staged source in the parse loop).
- [ ] Update the `_operation_owned_delta_paths` and
      `_try_finalize_pre_push_dirty_repair_state` docstrings to remove the
      staged delta and cite this thread; restore the `KXLaI`/`bbE6` fail-closed
      framing for unrelated staged dirt.
- [ ] The `KYd-r` test `test_pre_push_validation_finalize_commits_operation_owned_staged_dirt`
      now asserts fail-closed `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` (the
      documented defer); rename and update its docstring to record the defer
      rationale (mirroring the `bbE6` `KaUHP` defer test).
- [ ] Update any other finalize tests that queue a staged delta result to drop
      that queued result, keeping their asserted behavior where it still holds.
- [ ] Keep existing finalize tests green: unrelated-dirt fail-closed
      (`KXLaI`), working-tree-only fail-closed (`bbE6`), post-commit
      unowned-delta fail-closed (`KZP8f`), committed-delta-only post-commit
      re-validation (`Ka0aO`), rename-source (`KaAWk`), non-ASCII (`KaAWk`),
      untracked fail-closed (`cSj`/`Ka0aK`), agent-runtime exclusion (`Ka0aK`),
      no-anchor, delta-unavailable, malformed,
      policy/ownership/protected-scope/provider reason codes.
- [ ] Targeted lint + typecheck on touched files only.

## Implementation steps

1. Write the red regression test for unrelated staged dirt.
2. Run it, confirm it fails on the current (KYd-r) code — the finalize commits
   the unrelated staged path.
3. Remove the staged delta branch from `_operation_owned_delta_paths` (and its
   warning/`None` return); collapse the parse loop to the committed source only.
4. Update the two docstrings.
5. Update the `KYd-r` test to assert fail-closed with defer rationale
   (rename to `test_pre_push_validation_finalize_strands_operation_owned_staged_dirt_fail_closed`).
6. Update any other finalize tests that queue a staged delta result so they no
   longer queue it (their asserted behavior should still hold).
7. Re-run the full finalize + pre-push validation test files (TDD green).
8. Lint/typecheck touched files.
9. Write `plans/PRRT_kwDOSJAM6s6KdVXx_VALIDATION.md`.

## Verification commands (focused only — broad validation owned by AWF/GitHub)

- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`

## Pass criteria

- New regression test fails on the KYd-r code (commits unrelated staged dirt)
  and passes on the fixed code (fails closed).
- KYd-r test updated to assert fail-closed with documented defer.
- All other finalize + pre-push validation tests green.
- Lint/typecheck clean on touched files.

## Defer note

The KYd-r recovery (operation-owned staged dirt left by a failed `git commit`
after a successful `git add -A`) regresses to fail-closed
`VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`. This is the deliberate trade-off,
identical in shape to the `bbE6` `KaUHP` defer (failed `git add -A` leaving
operation-owned unstaged tracked edits) and the `cSj` `Ka0aK` defer (purely
untracked operation-owned output): a silent sweep of unrelated dirt into the PR
is worse than a visible fail-closed strand. Restoring KYd-r's recovery without
the over-broadening requires capturing the operation's attempted paths (the
`stage_paths` the commit sink computes, or the dirty set present immediately
after the agent run) and threading them to the pre-commit gate — a larger change
tracked as a deferred follow-up. The deferred case is covered by a renamed
fail-closed test so the regression is visible and the deferred follow-up has a
red-to-green target.
