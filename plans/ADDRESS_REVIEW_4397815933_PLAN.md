# Address Review Comment 4397815933 Plan

## Problem Statement and Scope

PR review comment `4397815933` targets a runtime validation-worktree reliability issue.

The scoped work is limited to:

- `src/awf/runtime/validation_worktree.py`
- this plan and validation artifact

## Requirements Checklist

- Remove the `awf.runtime.pr_monitor_runner.path_parsing` import dependency from
  `validation_worktree.py` to break a runtime circular-import path.
- Preserve porcelain parsing behavior by using local parser helpers that mirror the
  previous shared behavior.
- Keep ignore-path pathspecs as caller-provided values (including trailing slash
  intent) when snapshotting ignored files.
- Ensure ignored-root snapshot cleanup still runs when the initial check is otherwise
  clean (`check.clean == True`) and `ignore_ignored_paths_snapshot` is provided,
  so new ignored artifacts can be removed.
- Keep verification/rollback behavior unchanged for normal tracked/untracked dirty
  cleanup flows.
- Do not run broad AWF/GitHub-owned validation; use focused local checks only.

## Implementation Steps

1. In `src/awf/runtime/validation_worktree.py`, replace imports from
   `pr_monitor_runner.path_parsing` with local parsing helpers.
2. Update cleanup pathspec handling so `_snapshot_ignored_paths` receives
   `ignore_ignored_paths` verbatim (not normalized directory names).
3. Move the `check.clean` early-return to after ignored-root snapshot cleanup so
   snapshot-driven ignored deletions still execute.
4. Run focused unit tests for `tests/unit/runtime/test_validation_worktree.py`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  - Expected result: import/runtime changes are handled and only pre-existing isolated
    assertion mismatch remains; if that test fails due assertion shape, it should be
    limited to the explicit existing check around `args_ignored_snapshot`.
