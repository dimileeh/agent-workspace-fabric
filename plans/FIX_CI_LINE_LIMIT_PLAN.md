# Fix CI Line Limit Plan

## Problem Statement And Scope

The CI `python-full-coverage` job fails because the first-party file line limit
guard reports oversized test modules. The local focused repro currently reports:

- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`

Scope is limited to splitting these oversized test modules and preserving the
existing maintainability guard without disabling, skipping, or weakening it.

## Requirements Checklist

- Keep all first-party code files at or below the configured line limit.
- Preserve the existing test coverage and behavior from the oversized modules.
- Do not edit protected workflow, quality-gate, or configuration files.
- Run focused verification only; full AWF/GitHub validation remains managed by
  AWF after this agent phase.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Split late test cases from the oversized executor error-path test module into
   a new part module using existing part-file patterns.
2. Split failure-handling provisioner tests from the oversized provisioner test
   module into a new part module using existing part-file patterns.
3. Re-run the reported maintainability guard.
4. Run targeted pytest for the affected split test modules.
5. Create `FIX_CI_LINE_LIMIT_VALIDATION.md` with requirement-by-requirement
   status and focused command evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes and reports no oversized files.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py -q`
  - Passes for the executor split.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py -q`
  - Passes for the provisioner split.
