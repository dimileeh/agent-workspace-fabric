# PRRT_kwDOSJAM6s6KXLaI pre-push dirty finalize operation-owned scoping plan

## Problem statement
In `_run_pre_push_validation`
(`src/awf/runtime/pr_monitor_runner/pre_push_validation.py`), when the
pre-validation worktree check is dirty, `_try_finalize_pre_push_dirty_repair_state`
is gated only on `state is not None` (line 567) and then calls
`_commit_dirty_worktree`. That commit sink stages and commits **all**
non-ignored dirt in the worktree (see `_commit_dirty_worktree` in
`remote_repair.py`: `git status --porcelain --untracked-files=all` followed
by `git add -A -- <all remaining paths>`), with no operation-owned path
scoping. `MonitorState` carries no path information; `state is not None` only
means an active monitor loop, not that the current monitor operation
produced the dirt.

Consequence (raised in review thread PRRT_kwDOSJAM6s6KXLaI): unrelated files
introduced *after* the repair-start dirty guard
(`_pre_existing_dirty_repair_worktree_result`, which fail-closes with
`PRE_EXISTING_DIRTY_WORKTREE` if the tree is dirty before the agent runs) —
for example residue from a failed cleanup or another local process — bypass
the previous `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` fail-closed path and
can be swept into the PR. This violates the dirty-recovery plan's own
safety contract ("commit **safe** dirty repair output", "Keep all existing
fail-closed behavior for unrelated pre-existing dirt").

The operation-owned anchor that already exists in every repair caller is
`operation_start_head`: the repair-start dirty guard proves the tree was
clean at that SHA, so the operation's own committed delta is exactly
`git diff --name-only operation_start_head..HEAD`. Dirt confined to those
paths is operation-owned (the agent produced it from a clean baseline);
dirt on any path outside that delta is unrelated and must remain
fail-closed.

## Scope
- Gate `_try_finalize_pre_push_dirty_repair_state` on the dirty paths being a
  subset of the current operation's committed delta
  (`operation_start_head..HEAD`). If any dirty path is outside that delta,
  return `None` so the caller reuses the dirty check and reports
  `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` (preserving fail-closed).
- Thread `operation_start_head` through `_validated_git_push_result` →
  `_run_pre_push_validation_with_fix_passes` → `_run_pre_push_validation` →
  `_try_finalize_pre_push_dirty_repair_state`.
- Pass `operation_start_head` from the four repair callers that reach
  `_validated_git_push_result`:
  - `ci_ops._run_ci_fix` (already has `operation_start_head`),
  - `fix_cycle._run_fix_cycle` (already has `operation_start_head`),
  - `operator_hints._run_operator_hint_repair` (already has
    `operation_start_head`),
  - `remote_ops._run_sync_base` (capture the pre-merge HEAD as the
    operation's start anchor — the merge IS the operation there).
- No new abstractions, no unrelated refactor, no protected-file edits.

## Requirements checklist
- [ ] Add a regression test: dirty paths that are NOT a subset of the
      operation's committed delta keep the fail-closed
      `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` outcome (finalize skipped,
      `_commit_dirty_worktree` not called, validation not run).
- [ ] Add/keep a regression test: dirty paths that ARE a subset of the
      operation's committed delta are finalized (existing
      `test_validated_push_finalizes_monitor_dirty_state_before_validation`
      is extended/retained to thread `operation_start_head` and assert the
      delta scoping path).
- [ ] Confirm the new fail-closed test fails against the current code
      (TDD red).
- [ ] Implement the minimal fix: thread `operation_start_head` and scope
      the finalize on `set(check.paths) ⊆ operation_delta_paths`.
- [ ] Confirm the new + existing finalize tests pass (TDD green).
- [ ] Targeted lint + typecheck on touched files only.

## Implementation steps
1. Write the regression test for the unrelated-dirt fail-closed case.
2. Run it, confirm it fails (TDD red).
3. Thread `operation_start_head` through the pre-push validation call chain.
4. In `_try_finalize_pre_push_dirty_repair_state`, compute the operation
   delta paths (`git diff --name-only operation_start_head..HEAD`) and
   return `None` when any dirty path is outside that set.
5. Update the four callers to pass `operation_start_head` (capture
   pre-merge HEAD in `_run_sync_base`).
6. Re-run the new + existing finalize tests (TDD green).
7. Lint/typecheck the touched files.

## Verification commands (focused only — broad validation owned by AWF/GitHub)
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/remote_ops.py`

## Pass criteria
- New unrelated-dirt regression test fails on the unfixed code and passes on
  the fixed code.
- Existing finalize tests (finalize, no-op recheck, policy/ownership/
  protected-scope/provider-retry reason-code preservation) still pass.
- `test_pre_push_validation_pre_existing_dirty_blocks_before_validation`
  (state=None path) still passes.
- Lint/typecheck clean on touched files.
