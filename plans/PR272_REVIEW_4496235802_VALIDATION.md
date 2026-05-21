# PR272 Review 4496235802 Validation

Plan reference: `PR272_REVIEW_4496235802_PLAN.md`

## Requirement Status

- Regression test for `pushing` active validation redispatch: Complete.
  Added
  `test_pushing_candidate_redispatches_active_validation_recovery_without_preservation`
  in `tests/unit/control/test_worker.py`. It failed before the production fix
  with `_recover_preserved_active_execution` returning `False`.
- Include `WorkspaceStatus.pushing` in the active validation recovery fast-path:
  Complete. `_recover_preserved_active_execution` now treats `pushing` like
  `running` and `validating` when an active `worker_restart` validation recovery
  operation exists.
- Document worker-restart execution claim status invariant: Complete. Added a
  comment next to `_WORKER_RESTART_RECOVERY_EXECUTION_CLAIM_STATUSES` explaining
  that preserved `validating` and `pushing` workspaces are rewound to `running`
  before executor claim.
- Document omitted stale-failure salvage checks: Complete. Added a comment above
  `_ACTIVE_EXECUTION_STALE_FAILURE_BLOCKING_SALVAGE_CHECKS` explaining why
  `SALVAGE_BLOCKED` and `SALVAGE_NOT_POSSIBLE` must remain absent.
- Run targeted tests and lint: Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `src/awf/db/repositories.py`
- `tests/unit/control/test_worker.py`
- `plans/PR272_REVIEW_4496235802_PLAN.md`
- `plans/PR272_REVIEW_4496235802_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "pushing_candidate_redispatches_active_validation_recovery_without_preservation"`
  - Failed before the production fix, as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "redispatches_active_validation_recovery_without_preservation or validating_candidate_redispatches_active_running_validation_recovery"`
  - Passed: 2 passed, 256 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/db/repositories.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_validation_recovery or active_validation_recovery_without_preservation or validating_candidate_redispatches_active_running_validation_recovery"`
  - Passed: 5 passed, 253 deselected.

## Gaps

None.
