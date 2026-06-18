# PRRT_kwDOSJAM6s6KaUHP pre-push dirty finalize operation-owned unstaged paths plan

## Problem statement
Review thread `PRRT_kwDOSJAM6s6KaUHP` (PR #615,
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py:618`) reports that
the pre-push dirty finalize ownership gate only unions the operation's
*committed* delta (`git diff --name-status -z operation_start_head..HEAD`) with
its *staged* delta (`git diff --name-status -z --cached operation_start_head`).
When the repair operation's `_commit_dirty_worktree` returns `False` because
`git add -A` failed (see `remote_repair.py:359-368`), the repair output was
never staged and remains as unstaged working-tree changes, even though the
repair-start dirty guard proved the worktree was clean at
`operation_start_head` and so the operation owns those edits.

With both deltas empty (`operation_start_head..HEAD` empty because HEAD never
moved, `--cached operation_start_head` empty because `git add -A` failed),
`owned_delta_paths` is empty, so `unrelated_dirty = dirty_paths -
owned_delta_paths` carries every unstaged repair path, the finalize is skipped
(`return None`), and the monitor strands the operation's own repair output as
`VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.

The staged-delta union added for `PRRT_kwDOSJAM6s6KYd-r` does not cover this
case: those edits are not staged.

The operation owns not only committed and staged paths, but also the
*unstaged* working-tree paths it produced after the clean
`operation_start_head` anchor — the working-tree diff against
`operation_start_head` (`git diff --name-status -z operation_start_head`,
which compares the commit to the working tree and therefore includes both
staged and unstaged changes).

## Scope
- Extend `_operation_owned_delta_paths`
  (`src/awf/runtime/pr_monitor_runner/pre_push_validation.py`) so the
  operation-owned set is the union of:
  1. the committed delta: `git diff --name-status -z operation_start_head..HEAD`
  2. the staged delta: `git diff --name-status -z --cached operation_start_head`
  3. the working-tree delta: `git diff --name-status -z operation_start_head`
     (committed-vs-working-tree; superset of staged that also includes
     unstaged edits)
  All three commands run against the same anchor; if any fails the helper
  returns `None` (preserving the existing fail-closed delta-unavailable
  behavior).
- Update the docstring of `_operation_owned_delta_paths` and
  `_try_finalize_pre_push_dirty_repair_state` to describe the unstaged
  working-tree delta and cite this thread.
- No new abstractions, no unrelated refactor, no protected-file edits, no
  caller signature changes.

## Requirements checklist
- [ ] Add a regression test: dirty paths that are NOT in the committed or
      staged delta but ARE in the unstaged working-tree delta against
      `operation_start_head` are finalized (committed by
      `_commit_dirty_worktree`) and validation proceeds, instead of failing
      as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`. Currently fails on the
      unfixed code (TDD red).
- [ ] Keep existing finalize tests green, including:
      - `test_pre_push_validation_finalize_commits_operation_owned_staged_dirt`
        (staged-only case still works; working-tree diff is a superset).
      - `test_pre_push_validation_finalize_skips_unrelated_dirt_outside_operation_delta`
        (unrelated dirt outside all three deltas still fails closed).
      - `test_pre_push_validation_finalize_fail_closed_when_commit_introduces_unowned_paths`
        (post-commit re-validation still fails closed on unowned paths).
- [ ] Implement the minimal fix in `_operation_owned_delta_paths`: union the
      committed, staged, and unstaged working-tree deltas against
      `operation_start_head`.
- [ ] Confirm the new + existing finalize tests pass (TDD green).
- [ ] Targeted lint + typecheck on touched files only.

## Implementation steps
1. Write the unstaged-only-dirt regression test (queue empty committed delta,
   empty staged delta, and a non-empty working-tree delta result).
2. Run it, confirm it fails against the current code (TDD red).
3. Extend `_operation_owned_delta_paths` to also run
   `git diff --name-status -z operation_start_head` and union the result with
   the committed + staged sets; return `None` if that diff fails.
4. Re-run the new + existing finalize tests (TDD green).
5. Update docstrings to cite `PRRT_kwDOSJAM6s6KaUHP`.
6. Lint/typecheck the touched files.

## Verification commands (focused only — broad validation owned by AWF/GitHub)
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`

## Pass criteria
- New unstaged-only-dirt regression test fails on the unfixed code and passes
  on the fixed code.
- Existing finalize tests still pass.
- Lint/typecheck clean on touched files.
