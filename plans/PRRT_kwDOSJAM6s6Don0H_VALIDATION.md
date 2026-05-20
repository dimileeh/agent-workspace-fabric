# PRRT_kwDOSJAM6s6Don0H Validation

Plan reference: `PRRT_kwDOSJAM6s6Don0H_PLAN.md`

## Requirement Status

- Regression test for a `validating` stale-active candidate with a running-status
  preservation event and validation salvage event: Complete.
  - Evidence: `tests/unit/control/test_worker.py`
- Stale-active failure remains blocked when validation salvage can continue and
  preservation was recorded under another active execution status: Complete.
  - Evidence: `src/awf/control/worker.py` uses active-cycle preservation lookup for
    stale-failure salvage checks.
- Preserve event-floor protections against older execution cycles and operator refreshes:
  Complete.
  - Evidence: active salvage lookup floors at the latest non-active state transition and
    latest operator refresh.
- Preserve exact-status idempotency for preservation recording per active phase: Complete.
  - Evidence: preservation duplicate checks remain exact-status; adjacent regression passed.
- Run narrow relevant tests and record validation evidence: Complete.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "validating_candidate_blocks_stale_failure_with_running_preservation"`
  - Result: Passed, `1 passed, 260 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "validating_candidate_blocks_stale_failure_with_running_preservation or restart_recovery_records_preservation_once_per_active_phase or validating_candidate_reuses_running_validation_salvage_event or validating_candidate_redispatches_active_running_validation_recovery or preserved_active_validation_slot_exhaustion_after_grace_does_not_block_stale_failure"`
  - Result: Passed, `5 passed, 256 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py -q -k "active_execution_event_queries_accept_event_floor or stale_active_execution_can_fail_rejects_preserved_runtime or stale_active_execution_can_fail_ignores_salvage_for_other_status"`
  - Result: Passed, `3 passed, 48 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: Passed.

## Gaps

No gaps found.
