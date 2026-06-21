# PRRT_kwDOSJAM6s6K7p1 Fallback Head Plan

## Problem Statement and Scope

The PR review thread reports that `_repair_operation_start_head_result` can return a fallback repair start SHA without validating that the commit exists in the shared mirror. Scope is limited to the repair start-head fallback behavior in `remote_repair.py` and focused regression tests.

## Requirements Checklist

- Verify the review claim against the current implementation.
- Validate fallback start-head SHAs against the shared mirror before returning them.
- Fail closed with `_REPAIR_START_HEAD_UNAVAILABLE_REASON` when a fallback SHA is dangling or the mirror cannot be resolved.
- Preserve existing behavior for valid fallback SHAs and successful worktree `rev-parse HEAD`.
- Add focused regression tests for missing-worktree candidate fallback and failed-`rev-parse` status fallback.

## Implementation Steps

1. Add a small local helper inside `_repair_operation_start_head_result` to validate a fallback SHA with `mirror_path_for_worktree` and `_mirror_commit_object_exists`.
2. Use that helper for both fallback paths before returning the fallback SHA.
3. Return the existing start-head-unavailable failure result when fallback validation fails.
4. Add targeted unit tests in the existing PR monitor runner test file.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_repair.py`

Full AWF/GitHub validation is managed by AWF after agent completion.

## Assumptions/Changes

The relevant existing start-head helper tests live in `test_pr_monitor_runner_coverage_edges_part_008.py`, so the focused regressions were added there instead of `test_pr_monitor_runner_part_005.py`.
