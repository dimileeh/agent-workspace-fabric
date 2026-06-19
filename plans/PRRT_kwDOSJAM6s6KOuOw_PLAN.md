# PRRT_kwDOSJAM6s6KOuOw Plan

## Problem Statement and Scope

Address review thread `PRRT_kwDOSJAM6s6KOuOw`: validation fix-pass status recheck early stops can return from `run_validation_and_fix_cycle` without depositing required planning/conformance artifacts. Scope is limited to the two cited recheck windows before `validation_fix_agent_run` and before `validation_fix_commit`.

## Requirements Checklist

- Deposit planning artifacts before returning `stop=True` when the fix-pass agent-run status recheck fails.
- Deposit planning artifacts before returning `stop=True` when the fix-pass commit status recheck fails.
- Add or update focused regression coverage for both early-stop paths.
- Do not run broad AWF/GitHub-owned validation; use targeted tests only.

## Implementation Steps

1. Update the two early-return branches in `src/awf/control/executor/execution_validation.py` to call the existing validation-local deposit helper.
2. Extend the focused fix-pass status recheck tests in `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py` to assert a deposit happens on each path.
3. Run the narrow affected test file or selected tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py -q`

Pass criteria: targeted tests pass, and no broad validation suite is executed locally.
