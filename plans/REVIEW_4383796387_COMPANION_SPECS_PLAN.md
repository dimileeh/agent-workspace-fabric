# Review 4383796387 Companion Specs Plan

## Problem Statement and Scope

CodeRabbit reported duplicated optional companion env-secret policy setup in
`tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_012.py`.
The scope is limited to removing that local duplication while preserving the
existing test behavior. The related inline `state_reset` finding is already
addressed in the current checkout and is outside this change.

## Requirements Checklist

- Verify the duplicated companion setup is still present.
- Extract a small local helper for the shared backend optional env-secret specs.
- Update the affected tests to use the helper without weakening assertions.
- Run focused validation only; AWF/GitHub owns broad validation after agent completion.
- Commit the scoped fix locally without pushing or switching branches.

## Implementation Steps

1. Add a helper near the top of the test module that calls
   `executor_monitor_handoff.companion_specs_from_task_policy(...)`.
2. Replace each identical inline backend optional env-secret setup with the helper.
3. Run the targeted test module and a focused lint check for the touched file.
4. Record validation evidence in a matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_012.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_012.py`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase.
