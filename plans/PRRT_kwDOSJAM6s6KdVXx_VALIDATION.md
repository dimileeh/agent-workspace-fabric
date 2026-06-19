# PRRT_kwDOSJAM6s6KdVXx validation

## Plan reference
`plans/PRRT_kwDOSJAM6s6KdVXx_PLAN.md`

## Requirement-by-requirement status

- [x] Add a regression test (TDD red): a tracked file staged after the
      repair-start guard by an unrelated process (present in the staged delta
      but NOT committed) is NOT swept into the PR — the finalize skips and the
      push fails closed as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.
      Evidence: `test_pre_push_validation_finalize_skips_unrelated_staged_dirt`
      in `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`.
      Confirmed TDD red against the KYd-r code: `commit_dirty.assert_not_awaited()`
      failed (the unrelated staged path was committed via the staged-delta
      branch). Passes on the fixed code.
- [x] Remove the staged delta branch from `_operation_owned_delta_paths`
      (drop the `git diff --name-status -z --cached operation_start_head` diff,
      its `monitor.pre_push_dirty_finalize_staged_delta_unavailable` warning,
      and the staged source in the parse loop). Evidence:
      `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` — the
      `staged_result` block and its warning were removed; the helper now parses
      only the committed delta.
- [x] Update the `_operation_owned_delta_paths` and
      `_try_finalize_pre_push_dirty_repair_state` docstrings to remove the
      staged delta and cite this thread; restore the `KXLaI`/`bbE6` fail-closed
      framing for unrelated staged dirt. Evidence: both docstrings updated
      with the `PRRT_kwDOSJAM6s6KdVXx` citation and the rationale that the
      committed delta is the only operation-captured set.
- [x] The KYd-r test renamed to
      `test_pre_push_validation_finalize_strands_operation_owned_staged_dirt_fail_closed`
      and asserts fail-closed `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`
      (documented defer); docstring updated to record the defer rationale.
- [x] Update other finalize tests that queue a staged delta result to drop
      that queued result, keeping their asserted behavior where it still
      holds. Evidence: 8 tests updated
      (`test_validated_push_finalizes_monitor_dirty_state_before_validation`,
      `test_pre_push_validation_rechecks_tree_after_no_op_finalize`,
      `test_pre_push_validation_finalize_threads_remote_branch_and_url_to_commit_sink`,
      `test_pre_push_validation_finalize_fail_closed_when_commit_introduces_unowned_paths`,
      `test_pre_push_validation_finalize_ignores_working_tree_only_unowned_dirt`,
      `test_pre_push_validation_finalize_commits_operation_owned_rename_source_dirt`,
      `test_pre_push_validation_finalize_commits_operation_owned_non_ascii_dirt`,
      `test_pre_push_validation_finalize_skips_unrelated_working_tree_only_dirt`,
      `test_pre_push_validation_finalize_strands_operation_owned_unstaged_dirt_fail_closed`).
      The rename and non-ASCII tests were converted from a staged-delta
      scenario to a committed-delta scenario so they still exercise the
      `--name-status -z` path-representation concern (`KaAWk`) via the
      committed delta.
- [x] Keep existing finalize tests green. Evidence: full finalize file run
      below (28/28 passed), plus the rest of the pre-push validation suite
      (123 passed) and a broader pre-push/dirty slice (180 passed).
- [x] Targeted lint + typecheck on touched files only. Evidence below.

## Evidence (focused checks only — broad validation owned by AWF/GitHub)

TDD red (KYd-r code, new regression test):
```
$ uv run --python 3.12 --extra dev python -m pytest \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py::test_pre_push_validation_finalize_skips_unrelated_staged_dirt -q
...
E       AssertionError: Expected mock to not have been awaited. Awaited 1 times.
2026-06-18 ... [warning  ] monitor.pre_push_dirty_finalize_still_dirty paths=['unrelated/staged.log'] ...
1 failed
```

TDD green (fixed code, full finalize file):
```
$ uv run --python 3.12 --extra dev python -m pytest \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q
............................                          [100%]
28 passed in 29.74s
```

Other pre-push validation suites:
```
$ uv run --python 3.12 --extra dev python -m pytest \
    tests/unit/runtime/test_pr_monitor_pre_push_validation.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/ -q
........................................................................ [ 58%]
...................................................                      [100%]
123 passed in 119.89s
```

Broader pre-push/dirty slice (sanity):
```
$ uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/ -q -k "pre_push or finalize or dirty"
........................................................................ [ 40%]
........................................................................ [ 80%]
....................................                                     [100%]
180 passed, 2197 deselected in 164.26s
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
  `_operation_owned_delta_paths` no longer unions the live staged delta
  (removed the `git diff --name-status -z --cached operation_start_head` diff,
  its `monitor.pre_push_dirty_finalize_staged_delta_unavailable` warning, and
  the second source in the parse loop); the helper now parses only the
  committed delta. Docstrings of `_operation_owned_delta_paths` and
  `_try_finalize_pre_push_dirty_repair_state` updated to cite
  `PRRT_kwDOSJAM6s6KdVXx` and document the deliberate removal / defer. The
  post-commit re-validation comment was updated to drop the stale
  "committed + staged + working-tree deltas" wording.
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`:
  new `test_pre_push_validation_finalize_skips_unrelated_staged_dirt`
  regression; KYd-r test renamed to
  `test_pre_push_validation_finalize_strands_operation_owned_staged_dirt_fail_closed`
  with fail-closed assertions + defer rationale; the rename and non-ASCII
  tests converted from a staged-delta scenario to a committed-deltas
  scenario (still covering the `KaAWk` `--name-status -z` parsing); 8 other
  existing finalize tests updated to drop the queued staged-delta result and
  update comments.
- `plans/PRRT_kwDOSJAM6s6KdVXx_PLAN.md` / `plans/PRRT_kwDOSJAM6s6KdVXx_VALIDATION.md`.

## Gaps / defer

The KYd-r recovery (operation-owned staged dirt left by a failed `git commit`
after a successful `git add -A`) regresses to fail-closed
`VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`. This is the deliberate trade-off,
identical in shape to the `bbE6` `KaUHP` defer (failed `git add -A` leaving
operation-owned unstaged tracked edits) and the `cSj` `Ka0aK` defer (purely
untracked operation-owned output): a silent sweep of unrelated dirt into the PR
is worse than a visible fail-closed strand. Restoring KYd-r's recovery without
the over-broadening requires capturing the operation's attempted paths (the
`stage_paths` the commit sink computes, or the dirty set present immediately
after the agent run) and threading them to the pre-commit gate — a larger
change tracked as a deferred follow-up. The deferred case is covered by
`test_pre_push_validation_finalize_strands_operation_owned_staged_dirt_fail_closed`,
which asserts the fail-closed outcome so the regression is visible and the
deferred follow-up has a red-to-green target.
