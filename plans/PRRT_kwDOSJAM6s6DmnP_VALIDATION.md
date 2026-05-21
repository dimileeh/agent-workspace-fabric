# PRRT_kwDOSJAM6s6DmnP Validation

Plan reference: `PRRT_kwDOSJAM6s6DmnP_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving a `validating` candidate can see
  a current `ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED` event whose payload
  was written while the workspace was `running`.
- Complete: Added regression coverage for the exact running-to-validating
  restart path, where an active worker-restart validation operation exists but
  no `validating` preservation event has been written yet.
- Complete: Stale-active cleanup is blocked when validation recovery can still
  continue, and still allowed for no-executor or no-capacity cases covered by
  existing adjacent tests.
- Complete: The broad status matching remains limited to
  `ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED`; other salvage event lookups
  still use exact candidate status matching.
- Complete: Targeted worker recovery validation, lint, and mypy checks passed.

## Evidence

Changed files:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PRRT_kwDOSJAM6s6DmnP_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DmnP_VALIDATION.md`

TDD evidence:

- Failing before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_validating_candidate_reuses_running_validation_salvage_event -q`
- Failing before the active-recovery gate adjustment:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_validating_candidate_redispatches_active_running_validation_recovery -q`

Passing validation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "validating_candidate_reuses_running_validation_salvage_event or validating_candidate_redispatches_active_running_validation_recovery or preserved_active_clean_committed_non_running_work_rewinds_for_validation_salvage or preserved_active_rewound_validation_salvage_waits_without_duplicate_when_slots_full or preserved_active_validation_busy_worker_blocks_stale_failure_after_grace or preserved_active_validation_slot_exhaustion_after_grace_does_not_block_stale_failure or preserved_active_validation_salvage_without_executor_does_not_block_stale_failure" -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`

Broader check note:

- A full `tests/unit/control/test_worker.py -q` run was attempted and exposed
  existing pushed-branch PR handoff failures outside this validation-salvage
  status scope:
  `test_preserved_active_pushed_branch_lookup_falls_back_to_branch_name` and
  `test_preserved_active_adopted_sync_feature_pr_fork_head_repo_attaches_monitor`.
