# Review Thread PRRT_kwDOSJAM6s6CN1q3 Plan

## Problem Statement and Scope

The executor records setup dependency network retry events after setup and
pre-agent validation. That recording is observability-only, but an exception
from the DB session currently propagates through the broad agent-run try block
and can fail an otherwise runnable workspace before the agent starts.

Scope is limited to isolating failures from setup dependency network event
recording in `src/awf/control/executor.py` and adding a regression test for the
executor flow.

## Requirements Checklist

- Add a regression test proving setup dependency network event-recording
  failures do not mark the workspace failed when setup passed.
- Preserve the existing setup failure behavior and setup dependency network
  failure details.
- Log event-recording failures for diagnosis without blocking agent execution.
- Keep changes scoped to the executor path and relevant unit tests.

## Implementation Steps

1. Add a focused executor unit test that forces
   `_record_setup_dependency_network_events` to raise after setup succeeds and
   asserts the agent path still runs.
2. Update the executor to catch and log exceptions from setup dependency
   network event recording before checking `setup_result.all_passed`.
3. Run the focused test to confirm it fails before the fix and passes after.
4. Run a narrow unit-test surface for the touched executor behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery.py::<new-test> -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery.py -q`
  passes.
