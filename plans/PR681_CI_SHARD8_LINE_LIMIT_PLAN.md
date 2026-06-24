# PR681 CI Shard 8 Line Limit Plan

## Problem Statement And Scope

The latest PR #681 CI run no longer fails in the original aggregate coverage
threshold step. It now fails in `python-coverage-shards (8)` because
`tests/unit/service/test_status_parts/test_status_part_001.py` has grown to
1626 lines, violating the repository's 1500-line first-party file limit.

Scope is limited to splitting the oversized status test part into the existing
status part files. Do not change production behavior, weaken the line-limit
guard, or run broad local validation.

## Requirements Checklist

- Move focused status tests out of `test_status_part_001.py` until it is below
  the 1500-line limit.
- Preserve all moved behavioral assertions.
- Keep imports explicit and avoid duplicating helper implementations.
- Run focused local checks for the line-limit guard and affected status tests.
- Record validation evidence in `plans/PR681_CI_SHARD8_LINE_LIMIT_VALIDATION.md`.

## Implementation Steps

1. Move worker reaper heartbeat tests and recent orphan reaping tests from
   `test_status_part_001.py` into `test_status_part_003.py`.
2. Update `test_status_part_003.py` imports for the moved tests.
3. Run the line-limit guard and the affected status part tests.
4. Document the outcome in the validation file.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes locally.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_status_parts/test_status_part_001.py tests/unit/service/test_status_parts/test_status_part_003.py -q`
  - Passes locally.

Full AWF/GitHub validation remains managed by AWF/GitHub after agent completion.
