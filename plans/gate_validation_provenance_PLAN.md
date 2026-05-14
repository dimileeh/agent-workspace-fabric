# Gate Validation Provenance Plan

## Problem Statement And Scope

An unresolved PR review thread reports that `load_primary_failure_snapshot` attaches the latest failed validation run to any preserved primary failure, even when the primary failure is an agent or infrastructure failure. That can make later cleanup or stranding events report unrelated validation provenance as the primary cause.

Scope is limited to failure-causality snapshot construction and a regression test proving non-validation primary failures do not inherit unrelated failed validation runs.

## Requirements Checklist

- Preserve validation run and coverage provenance when the primary failure is `validation_failure`.
- Do not attach a latest failed validation run or coverage to a non-validation primary failure solely because one exists historically.
- Preserve any validation provenance already embedded in an existing primary failure payload.
- Keep existing primary failure reason/message/reason-code preservation behavior intact.
- Commit the fix locally without pushing or changing branches.

## Implementation Steps

1. Add a focused unit test for `load_primary_failure_snapshot` with an agent failure plus a historical failed validation run.
2. Confirm the new test fails before changing production code when practical.
3. Gate attachment of queried validation-run provenance to validation primary failures, while retaining embedded primary payload contents.
4. Run the focused test and an adjacent validation-preservation test.
5. Create `plans/gate_validation_provenance_VALIDATION.md` with requirement-by-requirement evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  - Passes all focused failure-causality tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_stale_active_execution_preserves_validation_failure_and_records_secondary_stale tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_runtime_stranding_preserves_provider_auth_primary_failure -q`
  - Confirms adjacent worker preservation behavior still passes.
