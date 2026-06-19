# PRRT_kwDOSJAM6s6KbbE6 validation

## Plan reference
`plans/PRRT_kwDOSJAM6s6KbbE6_PLAN.md`

## Requirement-by-requirement status

- [x] Add a regression test (TDD red): a tracked file modified after the
      repair-start guard by an unrelated process (present in the working-tree
      delta but NOT committed, NOT staged, and NOT untracked) is NOT swept
      into the PR — the finalize skips and the push fails closed as
      `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.
      Evidence: `test_pre_push_validation_finalize_skips_unrelated_working_tree_only_dirt`
      in `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`.
      Confirmed TDD red against the KaUHP code: `monitor.pre_push_dirty_finalize_still_dirty
      paths=['unrelated/lefover.log']` and `commit_dirty.assert_not_awaited()`
      failed (the unrelated path was committed). Passes on the fixed code.
- [x] Remove the working-tree delta branch from `_operation_owned_delta_paths`
      (drop the `git diff --name-status -z operation_start_head` diff and its
      `working_tree_delta_unavailable` warning / `None` return). Evidence:
      `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` — the
      `working_tree_result` block and its warning were removed; the helper now
      unions only the committed and staged deltas.
- [x] Update the `_operation_owned_delta_paths` and
      `_try_finalize_pre_push_dirty_repair_state` docstrings to remove the
      working-tree delta and cite this thread; restore the KXLaI fail-closed
      framing for unrelated working-tree-only tracked modifications. Evidence:
      both docstrings updated with the `PRRT_kwDOSJAM6s6KbbE6` citation and the
      rationale that committed + staged deltas are the operation-captured set.
- [x] Update existing finalize tests that queued a working-tree delta result
      to drop that queued result, keeping their asserted behavior where it
      still holds. Evidence: 7 tests updated
      (`test_validated_push_finalizes_monitor_dirty_state_before_validation`,
      `test_pre_push_validation_finalize_commits_operation_owned_staged_dirt`,
      `test_pre_push_validation_rechecks_tree_after_no_op_finalize`,
      `test_pre_push_validation_finalize_threads_remote_branch_and_url_to_commit_sink`,
      `test_pre_push_validation_finalize_fail_closed_when_commit_introduces_unowned_paths`,
      `test_pre_push_validation_finalize_commits_operation_owned_rename_source_dirt`,
      `test_pre_push_validation_finalize_commits_operation_owned_non_ascii_dirt`,
      `test_pre_push_validation_finalize_commits_operation_owned_untracked_dirt`,
      `test_pre_push_validation_finalize_excludes_agent_runtime_untracked_dirt`).
- [x] The KaUHP test now asserts fail-closed `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`
      (the documented defer); renamed and docstring updated to record the defer
      rationale. Evidence:
      `test_pre_push_validation_finalize_strands_operation_owned_unstaged_dirt_fail_closed`.
- [x] Keep existing finalize tests green. Evidence: full finalize file run
      below (26/26 passed), plus the rest of the pre-push validation suite
      (37 + 57 fix-pass parts).
- [x] Targeted lint + typecheck on touched files only. Evidence below.

## Evidence (focused checks only — broad validation owned by AWF/GitHub)

TDD red (KaUHP code, new regression test):
```
$ uv run --python 3.12 --extra dev python -m pytest \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py::test_pre_push_validation_finalize_skips_unrelated_working_tree_only_dirt -q
...
E       AssertionError: Expected 'not to be awaited.' ...
2026-06-18 ... [warning  ] monitor.pre_push_dirty_finalize_still_dirty paths=['unrelated/lefover.log'] ...
1 failed
```

TDD green (fixed code, full finalize file):
```
$ uv run --python 3.12 --extra dev python -m pytest \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q
..................................                          [100%]
26 passed in 27.73s
```

Other pre-push validation suites:
```
$ uv run --python 3.12 --extra dev python -m pytest \
    tests/unit/runtime/test_pr_monitor_pre_push_validation.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py -q
.....................................                       [100%]
37 passed in 34.05s
```

Fix-pass parts:
```
$ uv run --python 3.12 --extra dev python -m pytest \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/ -q
.........................................................     [100%]
57 passed in 56.20s
```

Lint:
```
$ uv run --python 3.12 --extra dev ruff check \
    src/awf/runtime/pr_monitor_runner/pre_push_validation.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py
All checks passed!
```

Typecheck:
```
$ uv run --python 3.12 --extra dev mypy \
    src/awf/runtime/pr_monitor_runner/pre_push_validation.py
Success: no issues found in 1 source file
```

## Files changed
- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`:
  `_operation_owned_delta_paths` no longer unions the unstaged working-tree
  delta (removed the `git diff --name-status -z operation_start_head` diff,
  its `monitor.pre_push_dirty_finalize_working_tree_delta_unavailable`
  warning, and the third source in the parse loop); docstrings of
  `_operation_owned_delta_paths` and `_try_finalize_pre_push_dirty_repair_state`
  updated to cite `PRRT_kwDOSJAM6s6KbbE6` and document the deliberate
  removal / defer.
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`:
  new `test_pre_push_validation_finalize_skips_unrelated_working_tree_only_dirt`
  regression; KaUHP test renamed to
  `test_pre_push_validation_finalize_strands_operation_owned_unstaged_dirt_fail_closed`
  with fail-closed assertions + defer rationale; 8 existing finalize tests
  updated to drop the working-tree-delta queued result and update comments.
- `plans/PRRT_kwDOSJAM6s6KbbE6_PLAN.md` / `plans/PRRT_kwDOSJAM6s6KbbE6_VALIDATION.md`.

## Gaps / defer

The KaUHP recovery (operation-owned unstaged tracked edits left by a failed
`git add -A`) regresses to fail-closed `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.
This is the deliberate trade-off documented in the plan: a silent sweep of
unrelated dirt into the PR is worse than a visible fail-closed strand. Restoring
KaUHP's recovery without the over-broadening requires capturing the operation's
attempted paths (the `stage_paths` the commit sink computes, or the dirty set
present immediately after the agent run) and threading them to the pre-commit
gate — a larger change tracked as a deferred follow-up. The deferred case is
covered by
`test_pre_push_validation_finalize_strands_operation_owned_unstaged_dirt_fail_closed`,
which asserts the fail-closed outcome so the regression is visible and the
deferred follow-up has a red-to-green target.
