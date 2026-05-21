# Preserved Active Recovery Redispatch Validation

Plan reference: `PRESERVED_ACTIVE_RECOVERY_REDISPATCH_PLAN.md`

## Requirement Status

- Preserve the existing running-workspace redispatch behavior: Complete.
  The existing redispatch tests still pass in the related recovery selection.
- For `validating` and `pushing` workspaces with active `worker_restart`
  validation recovery, persist a transition back to `running` before executor
  redispatch: Complete. Added a parameterized regression covering both statuses.
- Keep the recovery operation and existing salvage event lineage intact:
  Complete. The fix rewinds workspace state and clears the execution claim
  without replacing or cancelling the active recovery operation.
- Do not weaken existing stale-cleanup, executor-claim, or safety regression
  tests: Complete. Existing related worker and executor tests passed unchanged.
- Verify with a focused failing-then-passing unit test and run the narrow worker
  test selection: Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PRESERVED_ACTIVE_RECOVERY_REDISPATCH_PLAN.md`
- `plans/PRESERVED_ACTIVE_RECOVERY_REDISPATCH_VALIDATION.md`

Commands run:

- Failing before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "non_running_candidate_redispatches_active_validation_recovery_rewinds_to_running"`
  failed for both `validating` and `pushing` because the workspace status stayed
  non-running.
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "non_running_candidate_redispatches_active_validation_recovery_rewinds_to_running"`
  passed: 2 passed, 264 deselected.
- Related recovery selection:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py tests/unit/control/test_executor.py -q -k "worker_restart_recovery or redispatches_active_validation_recovery"`
  passed: 10 passed, 321 deselected.
- Existing running redispatch behavior:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "existing_validation_recovery_redispatch or existing_rebase_recovery_redispatches or non_running_candidate_redispatches_active_validation_recovery"`
  passed: 4 passed, 262 deselected.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.

## Gaps

None.
