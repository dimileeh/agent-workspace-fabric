# Comment 4587922231 Porcelain Parsing Plan

## Problem Statement and Scope

Review comment `issue:4587922231` reports that non-NUL Git porcelain path parsing helpers were copied into `src/awf/runtime/validation_worktree.py` even though equivalent helpers already exist in `src/awf/runtime/pr_monitor_runner/path_parsing.py`. The scope is to remove duplicated parser logic while preserving existing return types and behavior for both validation-worktree and PR-monitor callers.

## Requirements Checklist

- Centralize the shared C-quoted porcelain decoding, rename splitting, changed-path parsing, and untracked-path parsing logic in one runtime helper module.
- Keep `validation_worktree` tuple-returning behavior unchanged.
- Keep `pr_monitor_runner.path_parsing` list-returning wrapper behavior unchanged.
- Preserve existing tests and safety assertions; do not weaken review or validation worktree coverage.
- Run only focused local checks; full AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a focused regression test that proves validation-worktree and PR-monitor parsing go through the shared runtime helper surface.
2. Introduce a generic runtime porcelain parsing helper module.
3. Replace duplicate implementations in `validation_worktree.py` and `pr_monitor_runner/path_parsing.py` with imports/wrappers around the shared helper.
4. Run the targeted parser/helper tests and any narrow import/type smoke check needed for the touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_path_helpers.py -q`
  - Passes focused parser wrapper and compatibility tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_treats_untracked_paths_as_dirty tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_can_ignore_all_ignored_paths tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_reports_tracked_path_under_ignored_root -q`
  - Passes focused validation-worktree pre-check tests that exercise the shared parser call sites.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/git_porcelain.py src/awf/runtime/validation_worktree.py src/awf/runtime/pr_monitor_runner/path_parsing.py tests/unit/runtime/test_pr_monitor_path_helpers.py`
  - Reports no lint violations in touched files.

Full repository validation, coverage gates, frontend builds, and CI-equivalent checks are intentionally left to AWF/GitHub after the agent phase per workspace contract.

## Assumptions/Changes

- An exploratory broader `tests/unit/runtime/test_validation_worktree.py` command currently fails in cleanup tests around missing `restore_ref` handling. Those failures exercise cleanup policy paths not edited by this parser centralization change, so final verification uses the parser wrapper test file plus focused validation-worktree pre-check tests that cover the shared parser call sites.
