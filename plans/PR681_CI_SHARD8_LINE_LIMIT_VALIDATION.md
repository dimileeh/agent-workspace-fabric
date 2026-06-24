# PR681 CI Shard 8 Line Limit Validation

Plan reference: `plans/PR681_CI_SHARD8_LINE_LIMIT_PLAN.md`

## Requirement Status

- Move focused status tests out of `test_status_part_001.py`: Complete.
  - Moved worker reaper heartbeat tests and orphan reaping behavior tests into
    `tests/unit/service/test_status_parts/test_status_part_003.py`.
- Preserve moved behavioral assertions: Complete.
  - Test names and assertions were preserved; only imports and file location
    changed.
- Keep imports explicit and avoid duplicated helpers: Complete.
  - Reused existing helpers from `test_status_part_001.py` instead of copying
    helper implementations.
- Focused local checks only: Complete.
  - Ran the shard-8 failing line-limit test, affected status test files, and
    focused ruff on touched test files.
- Broad validation not run locally: Complete.
  - Full AWF/GitHub validation, coverage provenance, and merge gating remain
    managed by AWF after agent completion.

## Evidence

Changed files:

- `tests/unit/service/test_status_parts/test_status_part_001.py`
- `tests/unit/service/test_status_parts/test_status_part_003.py`
- `plans/PR681_CI_SHARD8_LINE_LIMIT_PLAN.md`
- `plans/PR681_CI_SHARD8_LINE_LIMIT_VALIDATION.md`

Focused commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: `1 passed in 0.43s`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_status_parts/test_status_part_001.py tests/unit/service/test_status_parts/test_status_part_003.py -q`
  - Result: `44 passed in 5.66s`
- `uv run --python 3.12 --extra dev ruff check tests/unit/service/test_status_parts/test_status_part_001.py tests/unit/service/test_status_parts/test_status_part_003.py`
  - Result: `All checks passed!`

Line counts after split:

- `tests/unit/service/test_status_parts/test_status_part_001.py`: 1215 lines
- `tests/unit/service/test_status_parts/test_status_part_003.py`: 620 lines

## Remaining Gaps

None for the saved plan. The full PR CI run is not executed locally by design;
AWF/GitHub owns broad validation after this agent phase.
