# PR #280 Monitor Restart Stranding Guard Plan

## Problem Statement and Scope

The CI failure is in
`TestRunOnceMonitorRecovery::test_fresh_worker_records_recovery_operation_when_resuming_monitoring_pr`
and reports
`assert 'STRANDED_WORKSPACE' is None` for
`operation.payload["runtime_stranding_reason"]` after resume recovery.

Scope is limited to control-worker stale `monitoring_pr` recovery so that a
fresh restart path does not inherit a previous `STRANDED_WORKSPACE` runtime
reason when no live claim exists.

## Requirements Checklist

- Reproduce the provided failing focused test before fixing.
- Keep remonitor behavior on fresh worker restart (resume call + operation/event
  creation) intact.
- Ensure `runtime_stranding_reason` remains `None` for fresh/reclaimed workspaces
  with no existing monitor/execution claim.
- Avoid broad worker refactors and keep behavior for truly stale claimed workspaces
  unchanged.
- Add/update regression coverage as part of the touched test scenario.
- Run focused repro and narrow checks after change.
- Create a validation file documenting requirement-by-requirement status and
  evidence.
- Commit the fix with a conventional commit message.

## Implementation Steps

1. Confirm the failing path in `worker.py` where stale `monitoring_pr` candidates
   are marked recoverable via `_record_recoverable_runtime_stranding`.
2. Introduce a claim check in the running-monitoring-open-pr recovery branch so
   it only runs when the candidate currently has an active monitor or execution
   claim.
3. Keep the branch’s existing recovery semantics (remonitor operation + monitor
   state preservation) untouched when a claim is present.
4. Re-run the focused failing test command.
5. Create/update the validation document with commands and outcomes.
6. Commit changes.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceMonitorRecovery::test_fresh_worker_records_recovery_operation_when_resuming_monitoring_pr -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  should pass for touched files.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  should pass or any documented type limitation must be noted.
