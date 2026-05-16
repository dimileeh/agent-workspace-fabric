# Review Thread PRRT_kwDOSJAM6s6CCcyg Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6CCcyg` reports that
`ControlWorker._record_preserved_active_execution_after_restart` manually bumps
`Workspace.version` before appending `workspace.active_execution_preserved_after_restart`.
`WorkspaceRepository.add_event` now reserves event orders by incrementing the
same version column, so this path can advance the version twice for one
preservation mutation.

Scope is limited to the preserved-active-execution restart recovery path and a
focused regression test proving one preservation appends one event and advances
workspace version by exactly one.

## Requirements Checklist

- Add or update a regression test that fails when preservation double-increments
  `Workspace.version`.
- Preserve the existing preserved-active-execution event payload, operation
  creation, subphase update, and stale event behavior.
- Remove only the redundant manual version bump from the preservation path.
- Run the narrow affected unit test(s) and formatting/lint checks justified by
  the touched files.

## Implementation Steps

1. Update an existing restart-recovery preservation test to capture the
   workspace version before preservation and assert the final version and event
   order advance once.
2. Run the focused test to confirm it fails against the current code.
3. Remove the redundant `ws.version += 1` in
   `src/awf/control/worker.py`.
4. Re-run the focused test and narrow lint for touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::<test> -q`
  passes after failing before the fix.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passes.
