# PRRT_kwDOSJAM6s6K9h7H Plan

## Problem Statement and Scope

The profile setup/pre_agent cleanup-failure path in `execution_flow.execute` repairs poisoned mirror hooks and then rethrows `ComposeExecCleanupError`. If setup created a commit while Git object writes were redirected outside the canonical mirror, the shared mirror branch ref can point at a missing HEAD object. The existing agent cleanup-failure path verifies and recovers missing HEAD before rethrowing; setup cleanup needs the same missing-HEAD guard.

Scope is limited to the setup/pre_agent `ComposeExecCleanupError` path and focused regression coverage.

## Requirements Checklist

- Verify HEAD existence after setup/pre_agent cleanup failures.
- Recover missing HEAD using the existing missing-git-object recovery helper before rethrowing the cleanup failure.
- Preserve the existing cleanup-failure terminal behavior and avoid double-marking recovery failures.
- Add focused regression coverage for the setup cleanup path.
- Run only targeted validation; full AWF/GitHub validation is managed after agent completion.

## Implementation Steps

1. Add a setup cleanup helper in `execution_flow.execute` that mirrors the missing-HEAD verification/recovery portion of the agent cleanup handler.
2. Invoke that helper after mirror hooks repair in the setup/pre_agent `ComposeExecCleanupError` handler and before rethrowing.
3. Add a targeted unit test that forces setup cleanup failure, simulates missing HEAD, and asserts recovery is attempted with a setup cleanup stage.
4. Run the focused test file or individual test needed to prove the behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_006.py -q -k setup_cleanup`
  - Passes with the new regression test.
