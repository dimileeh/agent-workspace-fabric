# Host-Port Retry Review Comment 4585090228 Plan

## Problem Statement And Scope

Address PR review comment `issue:4585090228` for the host-port retry path.

The actionable scope is limited to:

- retry workspace queue-decision `resource_summary` semantics when the source workspace has no `ResourceReservation`;
- a maintainability comment documenting the coupling between `stack_launch_started` and the immediate compose `launch()` call in the provisioner.

No branch changes, push, broad AWF/GitHub validation, full test suite, or coverage gate will be run in the agent phase.

## Requirements Checklist

- Add a regression test showing a retry from a source with no resource reservation still creates a retry reservation but records an empty queue-decision `resource_summary`.
- Update `retry_workspace_row` so `retry_resource_summary` is only populated from `retry_reservation.summary(...)` when a real source reservation existed.
- Preserve existing retry reservation creation for source workspaces without a reservation.
- Add a concise provisioner comment explaining that `stack_launch_started = True` must remain immediately adjacent to `launch()`.
- Run focused validation only for the changed behavior.

## Implementation Steps

1. Extend the existing legacy-no-reservation retry test to assert the retry queue decision has an empty resource summary.
2. Run that focused test to confirm the current behavior fails.
3. Change `src/awf/service/workspaces_retry.py` so the summary remains `{}` when `source_reservation is None`.
4. Add the launch-coupling comment in `src/awf/node/provisioner.py`.
5. Re-run the focused test and a narrow lint check for the touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_persist_reservation_when_source_has_none -q`
  - Passes after the implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py src/awf/node/provisioner.py tests/unit/service/test_workspace_retry_port.py`
  - Passes with no lint errors.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
