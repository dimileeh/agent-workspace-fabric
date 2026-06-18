# PRRT_kwDOSJAM6s6KYd-r pre-push dirty finalize operation-owned staged paths plan

## Problem statement
Review thread `PRRT_kwDOSJAM6s6KYd-r` (PR #615,
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py:632`) reports that
the pre-push dirty finalize ownership gate only considers the operation's
*committed* delta (`git diff --name-only operation_start_head..HEAD`). When
the repair operation's `_commit_dirty_worktree` returns `False` *before*
creating a commit (for example, `git commit` fails after the agent already
staged its edits via `git add -A`), `_run_ci_fix` (and the other repair
callers) still enter `_validated_git_push_result` with the operation's staged
edits still dirty in the worktree, but
`git diff --name-only operation_start_head..HEAD` is empty (HEAD never moved).

`_try_finalize_pre_push_dirty_repair_state` computes
`unrelated_dirty = dirty_paths - owned_delta_paths`; with an empty
`owned_delta_paths`, every dirty path is treated as unrelated and the
finalize is skipped (`return None`), so the operation's own residue still
fails as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` — exactly the
over-conservative fail-closed outcome the dirty-recovery plan was meant to
avoid for operation-owned dirt.

The operation owns not only the paths it committed, but also the paths it
*staged* (the cached diff against `operation_start_head`). The ownership gate
must include the staged paths the operation captured/attempted, not only
paths already committed.

## Scope
- Extend `_operation_owned_delta_paths`
  (`src/awf/runtime/pr_monitor_runner/pre_push_validation.py`) so the
  operation-owned set is the union of:
  1. the committed delta: `git diff --name-only operation_start_head..HEAD`
  2. the staged delta: `git diff --name-only --cached operation_start_head`
  Both commands are run against the same anchor; if either fails the helper
  returns `None` (preserving the existing fail-closed delta-unavailable
  behavior).
- No new abstractions, no unrelated refactor, no protected-file edits, no
  caller signature changes (`operation_start_head` is already threaded by
  the 3 repair callers that own it; sync_base keeps `None`).

## Requirements checklist
- [ ] Add a regression test: dirty paths that are NOT in the committed delta
      but ARE in the staged delta against `operation_start_head` are finalized
      (committed by `_commit_dirty_worktree`) and validation proceeds,
      instead of failing as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.
      Currently this fails on the unfixed code (TDD red).
- [ ] Add/keep a regression test: unrelated dirt outside both the committed
      and staged operation deltas still stays fail-closed as
      `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` (existing
      `test_pre_push_validation_finalize_skips_unrelated_dirt_outside_operation_delta`
      exercises the committed-delta empty case with no staged paths; keep it
      green).
- [ ] Implement the minimal fix in `_operation_owned_delta_paths`: union the
      committed delta with the staged delta against `operation_start_head`.
- [ ] Confirm the new + existing finalize tests pass (TDD green).
- [ ] Targeted lint + typecheck on touched files only.

## Implementation steps
1. Write the staged-only-dirt regression test (queue an empty committed
   delta result plus a non-empty staged delta result).
2. Run it, confirm it fails against the current code (TDD red).
3. Extend `_operation_owned_delta_paths` to also run
   `git diff --name-only --cached operation_start_head` and union the result
   with the committed-delta set.
4. Re-run the new + existing finalize tests (TDD green).
5. Lint/typecheck the touched files.

## Verification commands (focused only — broad validation owned by AWF/GitHub)
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`

## Pass criteria
- New staged-only-dirt regression test fails on the unfixed code and passes
  on the fixed code.
- Existing finalize tests (finalize, no-op recheck, unrelated-dirt
  fail-closed, no-anchor fail-closed, delta-unavailable fail-closed,
  policy/ownership/protected-scope/provider-retry reason-code preservation)
  still pass.
- Lint/typecheck clean on touched files.
