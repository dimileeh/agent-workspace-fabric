# PRRT_kwDOSJAM6s6Dafp3 Salvage Not Possible Atomicity Plan

## Problem Statement and Scope

The preserved active-execution `workspace.active_execution_salvage_not_possible`
recording path checks for an existing salvage event and then inserts a new event
after loading the workspace with a plain repository `get`. Concurrent workers can
therefore both pass the guard before either insert commits.

Scope is limited to the not-possible salvage recording path in
`src/awf/control/worker.py` and a focused regression test proving concurrent
recording serializes for the same workspace and preserved epoch.

## Requirements Checklist

- Add a regression test that fails when concurrent not-possible salvage writers
  can both perform the existing-event guard before the first writer commits.
- Acquire the workspace row lock before `_has_current_salvage_event` and before
  `add_event` in `_record_preserved_active_salvage_not_possible`.
- Preserve existing status and event-floor/idempotency behavior.
- Keep the change minimal and avoid altering unrelated salvage flows.
- Validate with the targeted unit test and relevant lint/test commands when
  practical.

## Implementation Steps

1. Add a focused PostgreSQL-backed concurrency regression in
   `tests/unit/control/test_worker.py` near the existing preserved-runtime
   salvage tests.
2. Confirm the regression fails against the current plain `repo.get` behavior.
3. Replace the workspace fetch in `_record_preserved_active_salvage_not_possible`
   with `WorkspaceRepository.get_for_update`.
4. Re-run the targeted test and focused worker test surface.
5. Record requirement-by-requirement validation in the matching validation file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k test_salvage_not_possible_recording_serializes_concurrent_events -q`
  - Passes with exactly one `workspace.active_execution_salvage_not_possible`
    event after concurrent writers.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Passes for the touched worker test module, or any failure is documented if
    outside this change.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passes without lint regressions.
