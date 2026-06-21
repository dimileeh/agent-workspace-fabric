# Plan: PRRT_kwDOSJAM6s6KcSj — do not own every untracked path

## Problem

Review thread `PRRT_kwDOSJAM6s6KcSj` on
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py:872`:

```python
owned_delta_paths = owned_delta_paths | set(check.untracked_paths)
```

This fold-in was added by commit `5705be3f5` (thread `PRRT_kwDOSJAM6s6Ka0aK`) to
recover operation-owned purely-untracked repair output that `git add -A` never
reached. It is over-broad in the same way the live working-tree delta was (the
defect `PRRT_kwDOSJAM6s6KbbE6` removed):

- The repair-start dirty guard (`_pre_existing_dirty_repair_worktree_result`)
  proves the worktree was clean at `operation_start_head` at **repair start**.
- `check.untracked_paths` is computed by `check_validation_worktree_clean` at
  **pre-push validation time**, which is later.
- Between those two events the agent CLI ran, protected-scope repair can run
  the agent CLI, and a failed cleanup or another local process can create an
  untracked file. That file is **not** captured/attempted by the operation,
  yet the fold-in treats it as operation-owned solely because it is untracked.
- `_commit_dirty_worktree` then stages it via `git add -A`, the post-commit
  committed-delta check (`_committed_delta_paths`) sees it as committed and
  confined to `owned_delta_paths`, and the untracked file is silently pushed
  instead of failing closed.

This is the exact over-broadening asymmetry `KbbE6` fixed for tracked working-tree
edits: silent sweep vs. visible fail-closed.

## Decision

Remove the `check.untracked_paths` fold-in from the ownership gate, mirroring
the `KbbE6` decision. The `Ka0aK` recovery (operation-owned purely-untracked
repair output left by a never-run `git add -A`) regresses to fail-closed
`VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` — the same tradeoff `KbbE6` accepted
for `KaUHP`. This is the safety-asymmetric correct choice: a silent sweep of
unrelated untracked dirt into the PR is worse than a visible fail-closed strand
that a human can recover from. Capturing the operation's attempted paths (the
reviewer's stated "captured/attempted" framing) is tracked as a deferred
follow-up that would restore `Ka0aK` without the over-broadening.

This matches the reviewer's explicit ask: "Please only include untracked paths
that were captured/attempted by the repair operation, or pass an allowed path
set into the commit sink." The committed and staged deltas ARE paths the
operation captured/attempted; `check.untracked_paths` is not.

## Scope

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`:
  - Remove the `owned_delta_paths | set(check.untracked_paths)` line and its
    comment block (lines ~857-872).
  - Update the `_try_finalize_pre_push_dirty_repair_state` and
    `_operation_owned_delta_paths` docstrings to drop the untracked fold-in
    framing and cite this thread, restoring the fail-closed framing for
    unrelated untracked paths.
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`:
  - Add a TDD-red regression test: a purely-untracked path created by an
    unrelated process after the repair-start guard is NOT swept into the PR;
    the finalize skips and the push fails closed as
    `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.
  - Convert the existing `Ka0aK` owned-untracked test
    (`test_pre_push_validation_finalize_commits_operation_owned_untracked_dirt`)
    from "commits" to "fail-closed defer", and update its docstring to record
    the defer rationale (mirror the `KaUHP`->`KbbE6` regression treatment).
  - Keep `test_pre_push_validation_finalize_excludes_agent_runtime_untracked_dirt`
    green: it already asserts fail-closed for a suppressed agent-runtime
    artifact whose `untracked_paths` is empty; removing the fold-in does not
    change that outcome (the owned set is empty either way).

## Requirements checklist

- [ ] TDD red: regression test asserting an unrelated purely-untracked path is
      NOT committed (finalize skips, push fails closed as
      `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`).
- [ ] Remove the `check.untracked_paths` fold-in line and its comment block.
- [ ] Update `_operation_owned_delta_paths` docstring to drop the untracked
      fold-in framing and cite `KcSj`.
- [ ] Update `_try_finalize_pre_push_dirty_repair_state` docstring: remove the
      untracked fold-in paragraph and cite `KcSj` for the fail-closed
      restoration; record the `Ka0aK` defer.
- [ ] Convert `test_pre_push_validation_finalize_commits_operation_owned_untracked_dirt`
      to fail-closed defer with updated docstring.
- [ ] Keep `test_pre_push_validation_finalize_excludes_agent_runtime_untracked_dirt`
      green unchanged (verify reasoning).
- [ ] Focused checks: `ruff check`, `mypy`, targeted `pytest` for the finalize
      test module. No broad suite (AWF/CI owns broad validation).

## Verification

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q
```

Pass criteria: new regression test green; converted defer test green; existing
agent-runtime exclusion test green; ruff/mypy clean.
