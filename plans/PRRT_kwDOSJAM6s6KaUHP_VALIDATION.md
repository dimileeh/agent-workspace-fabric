# PRRT_kwDOSJAM6s6KaUHP validation

## Plan reference
`plans/PRRT_kwDOSJAM6s6KaUHP_PLAN.md`

## Requirement-by-requirement status

- [x] Add a regression test: dirty paths that are NOT in the committed or
      staged delta but ARE in the unstaged working-tree delta against
      `operation_start_head` are finalized (committed by
      `_commit_dirty_worktree`) and validation proceeds, instead of failing
      as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.
      Evidence: `test_pre_push_validation_finalize_commits_operation_owned_unstaged_dirt`
      in `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`.
      Confirmed TDD red against the unfixed code:
      `monitor.pre_push_dirty_finalize_skipped_unrelated_dirt dirty_paths=['src/fix.py'] ... unrelated_dirty=['src/fix.py']`,
      result `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`. Passes on the fixed
      code.
- [x] Keep existing finalize tests green. Evidence: full file run below
      (22/22 passed), including the staged-only, unrelated-dirt fail-closed,
      and post-commit unowned-delta fail-closed regressions.
- [x] Implement the minimal fix in `_operation_owned_delta_paths`: union the
      committed, staged, and unstaged working-tree deltas against
      `operation_start_head`. Evidence:
      `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` — third
      `git diff --name-status -z operation_start_head` diff added with a
      dedicated `monitor.pre_push_dirty_finalize_working_tree_delta_unavailable`
      warning + `None` return on failure; docstrings of
      `_operation_owned_delta_paths` and
      `_try_finalize_pre_push_dirty_repair_state` updated to cite
      `PRRT_kwDOSJAM6s6KaUHP`.
- [x] Confirm the new + existing finalize tests pass (TDD green). Evidence
      below.
- [x] Targeted lint + typecheck on touched files only. Evidence below.

## Evidence (focused checks only — broad validation owned by AWF/GitHub)

TDD red (unfixed code, new regression test):
```text
$ uv run --python 3.12 --extra dev python -m pytest \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py::test_pre_push_validation_finalize_commits_operation_owned_unstaged_dirt -q
...
E       AssertionError: assert False is True
monitor.pre_push_dirty_finalize_skipped_unrelated_dirt dirty_paths=['src/fix.py'] ... unrelated_dirty=['src/fix.py']
1 failed
```

TDD green (fixed code, full finalize file):
```text
$ uv run --python 3.12 --extra dev python -m pytest \
    tests/unit/runtime/test_pr_monitor_pre_push_validation.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q
...........................................                              [100%]
43 passed in 42.19s
```

Edges/cleanup regressions:
```text
$ uv run --python 3.12 --extra dev python -m pytest \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py -q
....                                                                     [100%]
4 passed in 3.09s
```

Lint:
```text
$ uv run --python 3.12 --extra dev ruff check \
    src/awf/runtime/pr_monitor_runner/pre_push_validation.py \
    tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py
All checks passed!
```

Typecheck:
```text
$ uv run --python 3.12 --extra dev mypy \
    src/awf/runtime/pr_monitor_runner/pre_push_validation.py
Success: no issues found in 1 source file
```

## Files changed
- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`:
  `_operation_owned_delta_paths` now unions the committed, staged, and
  unstaged working-tree deltas; docstrings updated.
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`:
  new `test_pre_push_validation_finalize_commits_operation_owned_unstaged_dirt`
  regression; existing finalize tests updated to queue the third
  working-tree-delta result.
- `plans/PRRT_kwDOSJAM6s6KaUHP_PLAN.md` / `plans/PRRT_kwDOSJAM6s6KaUHP_VALIDATION.md`.

## Gaps
None.
