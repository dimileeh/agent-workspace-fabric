# Monitor Handoff Line Limit Plan

## Problem Statement and Scope

CI fails `test_first_party_code_files_stay_under_line_limit` because `src/awf/control/executor/monitor_handoff.py` has 1,589 lines, exceeding the 1,500-line first-party file cap. The fix must reduce the file size without weakening the maintainability check or changing monitor handoff behavior.

Scope is limited to decomposing the monitor handoff implementation and preserving existing tests that exercise companion environment secret refresh and resume behavior.

## Requirements Checklist

- Keep `monitor_handoff.py` under 1,500 lines.
- Do not disable, skip, or weaken the maintainability guardrail.
- Preserve existing companion env secret resume behavior and private helper compatibility used by current unit tests.
- Keep changes scoped to the monitor handoff decomposition and required plan/validation docs.
- Run focused verification only; full AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Extract Compose payload load/dump, atomic write, and environment list/map mutation helpers from `monitor_handoff.py` into a focused companion environment helper module.
2. Import the extracted helpers back into `monitor_handoff.py` so existing call sites and unit tests can continue to reference the same private names.
3. Remove imports from `monitor_handoff.py` that are only needed by the extracted helpers.
4. Run the provided focused maintainability repro and the targeted unit tests that cover the extracted helpers.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized files.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_011.py::test_present_optional_companion_env_secret_refs_preserves_empty_source tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_012.py -q`
  - Passes, showing companion env secret helper behavior is preserved.
