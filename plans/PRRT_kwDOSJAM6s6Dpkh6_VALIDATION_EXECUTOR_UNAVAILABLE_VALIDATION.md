# PRRT_kwDOSJAM6s6Dpkh6 Validation Executor Unavailable Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6Dpkh6_VALIDATION_EXECUTOR_UNAVAILABLE_PLAN.md`

## Requirement Status

- Reproduce the missing guard with a regression test: Complete.
  Updated `test_preserved_active_validation_salvage_without_executor_blocks_stale_cleanup`
  failed before the implementation because the workspace became `failed`.
- Record `workspace.active_execution_salvage_blocked` with
  `blocked_reason == "validation_executor_unavailable"`: Complete.
  The no-executor validation salvage branch now records blocked salvage through the
  existing `_record_preserved_active_salvage_blocked` helper.
- Return `True` from preserved active recovery in that condition: Complete.
  `_recover_preserved_active_execution` now returns after recording the blocked event,
  preventing same-cycle stale cleanup.
- Preserve dispatchable validation and slot-exhaustion behavior: Complete.
  Adjacent targeted tests pass.
- Commit only files changed for this review thread: Complete.
  Changed files are limited to `src/awf/control/worker.py`,
  `tests/unit/control/test_worker.py`, and these plan/validation docs.

## Evidence

- Initial red command:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k preserved_active_validation_salvage_without_executor_blocks_stale_cleanup -q`
  failed with `ws.status == "failed"` before the worker change.
- Final targeted tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_validation_salvage_without_executor or preserved_active_committed_work_without_executor or preserved_active_validation_slot_exhaustion_after_grace_does_not_block_stale_failure or preserved_active_validation_busy_worker_blocks_stale_failure_after_grace" -q`
  passed with `4 passed, 259 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.

## Gaps

None.
