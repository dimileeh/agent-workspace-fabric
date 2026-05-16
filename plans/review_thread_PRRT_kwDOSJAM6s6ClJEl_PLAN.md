# PRRT_kwDOSJAM6s6ClJEl Plan

## Problem Statement and Scope

The executor chooses a workspace validation tier before pending operator
revalidation operations have completed. The current tier helper only considers
previously succeeded validate operations, so a pending `requested_tier: 3`
validate operation can be finished with a lower profile/task tier.

Scope is limited to validation-tier selection for validate operations and a
focused regression test.

## Requirements Checklist

- Add a regression test proving active pending/running validate operation
  payloads raise the effective validation tier.
- Preserve existing behavior where successful validate operations can raise the
  tier from either `result` or `payload`.
- Ignore non-validate operations and inactive failed/cancelled validate
  operations when computing requested operation tiers.
- Keep profile and task-class tier floors unchanged.

## Implementation Steps

1. Add a failing unit test in `tests/unit/control/test_executor_coverage_edges.py`
   for a pending validate operation with `requested_tier: 3`.
2. Run the focused test to confirm the current helper ignores the pending tier.
3. Update `src/awf/control/executor.py` so validation-tier selection includes
   active validate operation payload tiers while preserving succeeded-operation
   result/payload support.
4. Run the focused test and a nearby helper test surface.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py::test_validation_tier_for_workspace_uses_active_validate_operation_payload_tier -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py::test_validation_tier_for_workspace_uses_successful_validate_operation_tier tests/unit/control/test_executor_coverage_edges.py::test_validation_tier_for_workspace_uses_active_validate_operation_payload_tier tests/unit/control/test_executor_coverage_edges.py::test_validation_tier_for_workspace_uses_task_class_floor -q`

Pass criteria: the regression fails before the implementation change, then all
listed focused tests pass after the implementation.
