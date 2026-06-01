# PR349 Path Parsing Untracked Validation

Plan reference: `PR349_PATH_PARSING_UNTRACKED_PLAN.md`

## Requirement Status

- Complete: `_untracked_paths_from_porcelain` and `_untracked_paths_from_porcelain_z`
  now return only porcelain `??` entries by default.
- Complete: validation worktree checks still treat ignored `!!` entries as dirty
  by opting into ignored-entry parsing explicitly.
- Complete: ignored-entry inclusion is explicit through
  `include_ignored=True` on the shared non-NUL porcelain parser.
- Complete: focused parser tests now assert ignored entries are excluded from
  PR monitor untracked helpers and available only through the explicit opt-in.
- Complete: only targeted tests and narrow lint were run locally; full AWF/GitHub
  validation remains managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/git_porcelain.py`
- `src/awf/runtime/validation_worktree.py`
- `src/awf/runtime/pr_monitor_runner/path_parsing.py`
- `tests/unit/runtime/test_pr_monitor_path_helpers.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_path_helpers.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py::test_git_push_and_porcelain_helpers_cover_clean_rename_and_invalid_lines tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_treats_ignored_paths_as_dirty -q`
  - Result: `9 passed in 1.42s`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/git_porcelain.py src/awf/runtime/validation_worktree.py src/awf/runtime/pr_monitor_runner/path_parsing.py tests/unit/runtime/test_pr_monitor_path_helpers.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py`
  - Result: `All checks passed!`

## Remaining Gaps

None. Full validation, coverage, and merge-gate provenance are intentionally
left to AWF/GitHub after this agent phase.
