# REVIEW_PRRT_kwDOSJAM6s6Dw63w Salvage Monitor Recovery Validation

Plan reference:
`REVIEW_PRRT_kwDOSJAM6s6Dw63w_SALVAGE_MONITOR_RECOVERY_PLAN.md`

## Requirement Status

- Regression for historical salvage monitor attach: Complete.
  Added
  `test_historical_salvage_monitor_attach_does_not_trigger_future_recovery_cooldown`
  in `tests/unit/control/test_worker.py`. It failed before the implementation
  because the remonitor operation was stamped with
  `ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED`.
- Preserve first salvage handoff behavior: Complete.
  The existing fresh handoff and persisted cooldown tests still pass.
- Keep change local: Complete.
  Implementation is limited to `src/awf/control/worker.py`, the focused test,
  and plan/validation docs.
- Do not weaken existing tests: Complete.
  No assertions were removed or relaxed.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "historical_salvage_monitor_attach_does_not_trigger_future_recovery_cooldown"`
  - Before implementation: failed with
    `ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED` in the new remonitor payload.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_pr_handoff_attaches_one_monitor_after_restart or persisted_salvage_monitor_resume_cooldown_survives_worker_restart or historical_salvage_monitor_attach_does_not_trigger_future_recovery_cooldown"`
  - Passed: `3 passed, 285 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.
