# CI GC Line Limit Fix Validation

Plan reference: `plans/ci_gc_line_limit_PLAN.md`

## Requirement status

- Keep the line-limit check intact and make every first-party file stay at or
  below 1,500 lines: Complete.
- Preserve `awf.service.gc` import compatibility for existing callers and
  tests: Complete.
- Move whole test blocks at stable function boundaries into sibling test files:
  Complete.
- Do not alter GC behavior while decomposing model/data containers: Complete.
- Run only focused local verification; leave full AWF/GitHub validation to AWF:
  Complete.
- Commit the fix locally with a conventional commit message: Complete after
  local commit.

## Evidence

Files changed:

- `src/awf/service/gc.py`
- `src/awf/service/gc_classify.py`
- `src/awf/service/gc_results.py`
- `tests/unit/runtime/test_monitor_completion_gc.py`
- `tests/unit/runtime/test_monitor_completion_gc_part_002.py`
- `tests/unit/service/test_gc_parts/test_gc_part_001.py`
- `tests/unit/service/test_gc_parts/test_gc_part_002.py`
- `tests/unit/service/test_gc_parts/test_gc_part_004.py`
- `tests/unit/service/test_gc_parts/test_gc_part_005.py`

Focused verification run:

- `uv run --python 3.12 --extra dev ruff check --fix src/awf/service/gc.py src/awf/service/gc_classify.py src/awf/service/gc_results.py tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_monitor_completion_gc_part_002.py tests/unit/service/test_gc_parts/test_gc_part_001.py tests/unit/service/test_gc_parts/test_gc_part_002.py tests/unit/service/test_gc_parts/test_gc_part_004.py tests/unit/service/test_gc_parts/test_gc_part_005.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/service/gc.py src/awf/service/gc_classify.py src/awf/service/gc_results.py tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_monitor_completion_gc_part_002.py tests/unit/service/test_gc_parts/test_gc_part_001.py tests/unit/service/test_gc_parts/test_gc_part_002.py tests/unit/service/test_gc_parts/test_gc_part_004.py tests/unit/service/test_gc_parts/test_gc_part_005.py`
  - Passed: `9 files already formatted`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passed: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py tests/unit/service/test_gc_parts/test_gc_part_002.py tests/unit/service/test_gc_parts/test_gc_part_004.py tests/unit/service/test_gc_parts/test_gc_part_005.py -q`
  - Passed: `79 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_monitor_completion_gc_part_002.py -q`
  - Passed: `26 passed`.
- `uv run --python 3.12 --extra dev mypy src/awf/service/gc.py src/awf/service/gc_classify.py src/awf/service/gc_results.py src/awf/service/orphans.py`
  - Passed: `Success: no issues found in 4 source files`.

Line-count evidence after the fix:

- `src/awf/service/gc.py`: 1,316 lines.
- `tests/unit/runtime/test_monitor_completion_gc.py`: 1,489 lines.
- `tests/unit/service/test_gc_parts/test_gc_part_001.py`: 1,467 lines.
- `tests/unit/service/test_gc_parts/test_gc_part_002.py`: 1,479 lines.

Full AWF/GitHub validation, including full coverage gates and CI-equivalent
surfaces, was not run locally per the AWF workspace contract and is managed by
AWF after agent completion.

## Gaps

No planned requirements are partial or missing.
