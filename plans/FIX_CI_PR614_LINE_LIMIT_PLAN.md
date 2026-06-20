# Fix CI PR 614 Line Limit Plan

## Problem Statement and Scope

PR #614 fails the `python-coverage-shards (8)` GitHub Actions check because
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`
finds `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
at 1543 lines, above the 1500-line first-party file limit.

Scope is limited to splitting that oversized test file without changing
production behavior, CI configuration, or the maintainability gate.

## Requirements Checklist

- Preserve all existing test behavior and assertions.
- Reduce `test_pr_monitor_runner_part_005.py` below 1500 lines.
- Keep the split local to adjacent `test_pr_monitor_runner_parts` files.
- Do not weaken, skip, or disable the failing check.
- Run focused verification only; broad AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Move a coherent helper-test class from `test_pr_monitor_runner_part_005.py`
   into a new adjacent part file with minimal imports.
2. Remove now-unused imports from `test_pr_monitor_runner_part_005.py`.
3. Run the failing maintainability test directly.
4. Run the moved tests directly to confirm behavior is preserved.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes and reports no oversized first-party files.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_011.py -q`
  passes for the moved notification/grace helper tests.
