# Fix CI Executor Error Paths Line Limit Plan

## Problem Statement and Scope

CI fails because `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
has 1540 lines, exceeding the first-party maintainability guard limit of 1500 lines.

Scope is limited to decomposing that test shard so the existing maintainability
guard passes without weakening or skipping the check.

## Requirements Checklist

- Keep the maintainability guard intact.
- Move existing test coverage without changing executor behavior under test.
- Keep all first-party code files at or below 1500 lines.
- Run focused validation only; AWF/GitHub owns broad validation after agent completion.
- Commit the local fix without pushing or changing branches.

## Implementation Steps

1. Split one or more tests from `test_executor_error_paths_part_013.py` into a new
   pytest-discoverable shard under `tests/unit/control/test_executor_error_paths_parts/`.
2. Reuse existing helpers and fixtures from the existing shard/module pattern.
3. Run the focused line-limit repro command.
4. Run the focused pytest nodes for the moved test and the remaining source shard.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized files.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_014.py -q`
  - Passes, proving the original shard and moved tests still run.

Broad AWF/GitHub validation is intentionally not run locally per workspace contract.
