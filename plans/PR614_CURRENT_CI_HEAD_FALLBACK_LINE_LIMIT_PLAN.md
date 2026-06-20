# PR614 Current CI Head Fallback Line Limit Plan

## Problem Statement and Scope

PR #614 has failing CI on the current head. Focused inspection found two live local
failures in the PR monitor repair-start fallback tests and a still-live
maintainability failure because
`tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
exceeds the 1500-line first-party file limit.

Scope is limited to the failing repair-start fallback behavior, moving existing
tests to satisfy the line limit, and focused verification. Broad AWF/GitHub
validation remains owned by AWF after agent completion.

## Requirements Checklist

- Preserve AWF branch ownership: do not switch branches, push, rebase, or run
  broad CI-equivalent validation.
- Fix `_repair_operation_start_head_result` so no-mirror fallback validation
  still honors the `verify_head_object_exists` guard used by tests and callers.
- Keep the repair-start fallback tests behaviorally meaningful.
- Split the oversized part 008 test file without changing test assertions.
- Add required validation documentation after implementation.
- Commit the scoped fix locally with a conventional commit message.

## Implementation Steps

1. Update fallback validation in `src/awf/runtime/pr_monitor_runner/remote_repair.py`
   so missing-worktree/no-mirror fallback uses `verify_head_object_exists` and
   existing worktree/no-mirror fallback continues using an explicit commit-object
   check.
2. Move the tail tests from part 008 into a new
   `test_pr_monitor_runner_coverage_edges_part_030.py` file with only the imports
   needed by those moved tests.
3. Run the focused repair-start fallback tests and the maintainability line-limit
   test.
4. Record results in a validation document.
5. Commit the code, moved tests, plan, and validation.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 pytest <two failing repair-start fallback tests> -q`
  passes.
- `uv run --python 3.12 pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes.
- `wc -l` confirms the split files are each below 1500 lines.
- Full AWF/GitHub validation is not run locally; AWF owns broad validation after
  agent completion.
