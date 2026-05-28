# PR292 Line Limit Plan

## Problem statement and scope

PR #292 fails CI because `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py` has 1,665 lines, exceeding the first-party file limit enforced by `tests/unit/test_core_decomposition_maintainability.py`.

Scope is limited to decomposing that oversized test shard without weakening the maintainability gate or changing executor behavior.

## Requirements checklist

- Keep the line-limit check unchanged.
- Split the oversized test file so every first-party file is below 1,500 lines.
- Preserve pytest discovery and behavior for the moved tests.
- Run focused validation only; full AWF/GitHub validation remains managed by AWF after agent completion.
- Commit the fix locally without switching branches or pushing.

## Implementation steps

1. Move the trailing companion-secret PR monitor tests from `test_executor_error_paths_part_006.py` into a new adjacent test shard.
2. Import the existing fixtures and helpers from part 006 into the new shard instead of duplicating setup.
3. Re-run the focused line-limit repro.
4. Run the moved test shard directly to confirm pytest discovery and behavior.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized files.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_010.py -q`
  - Passes, proving the moved tests are still collected and executable.
