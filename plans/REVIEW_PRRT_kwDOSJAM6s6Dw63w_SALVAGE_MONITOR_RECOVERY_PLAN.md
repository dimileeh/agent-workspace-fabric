# REVIEW_PRRT_kwDOSJAM6s6Dw63w Salvage Monitor Recovery Plan

## Problem Statement And Scope

The PR review reports that monitor recovery payloads classify any future
`monitoring_pr` restart as active-execution salvage whenever the workspace has
any historical salvage event. The fix is scoped to monitor recovery payload
classification and the downstream salvage resume cooldown path in
`src/awf/control/worker.py`.

## Requirements Checklist

- Add a regression test proving a historical
  `ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED` event does not mark unrelated
  later monitor recovery operations as salvage.
- Preserve the existing first salvage handoff behavior: a fresh salvage attach
  before the first monitor recovery still carries
  `active_execution_salvage_reason_code`.
- Keep the change local to worker monitor recovery helpers and tests.
- Do not weaken existing monitor recovery, claim cleanup, or cooldown tests.

## Implementation Steps

1. Add a failing unit regression around monitor recovery after an older salvage
   attach has already been followed by a prior `workspace.monitor_recovery_started`
   event.
2. Update the monitor recovery payload helper to derive salvage context only
   from events newer than the current recovery floor, using prior monitor
   recovery events and current `monitoring_pr` state entry as the floor.
3. Run the new focused test to confirm it fails before the implementation and
   passes afterward.
4. Run the nearby worker monitor recovery tests that cover fresh salvage
   handoff cooldown behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "historical_salvage_monitor_attach_does_not_trigger_future_recovery_cooldown"` fails before implementation and passes afterward.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_pr_handoff_records_monitor_recovery or persisted_salvage_monitor_resume_cooldown_survives_worker_restart or historical_salvage_monitor_attach_does_not_trigger_future_recovery_cooldown"` passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py` passes.
