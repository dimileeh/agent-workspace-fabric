# PR349 Path Parsing Untracked Plan

## Problem Statement And Scope

Greptile noted that PR monitor helpers named `_untracked_paths_from_porcelain`
and `_untracked_paths_from_porcelain_z` now include ignored `!!` entries. These
helpers are re-exported and can surprise future callers that expect only
untracked `??` paths.

Scope is limited to preserving the untracked-only helper contract while keeping
validation worktree code explicit where it intentionally treats ignored entries
as dirty validation artifacts.

## Requirements Checklist

- Keep `_untracked_paths_from_porcelain` and `_untracked_paths_from_porcelain_z`
  limited to `??` untracked status entries.
- Preserve validation worktree behavior that treats ignored entries as dirty
  unless they are explicitly ignored by baseline policy.
- Make ignored-entry inclusion explicit in shared porcelain parsing APIs.
- Update focused regression tests for the parser contract.
- Run only targeted tests for the changed parser and validation-worktree
  behavior; broad AWF/GitHub validation remains owned by AWF after completion.

## Implementation Steps

1. Add an explicit opt-in flag to the shared non-NUL porcelain untracked parser
   for callers that need ignored `!!` entries included.
2. Update validation worktree cleanliness checks to opt in to ignored entries.
3. Restore PR monitor path parsing helpers to untracked-only behavior.
4. Update focused tests that currently expect ignored entries from untracked
   helpers.
5. Commit the scoped change locally with a conventional commit message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_path_helpers.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py::test_git_push_and_porcelain_helpers_cover_clean_rename_and_invalid_lines tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_treats_ignored_paths_as_dirty -q`

Pass criteria: the targeted parser contract tests and the validation ignored-path
regression all pass.
