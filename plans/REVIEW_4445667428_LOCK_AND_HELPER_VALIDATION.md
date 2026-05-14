# Review 4445667428 Lock And Helper Validation

Plan reference: `plans/REVIEW_4445667428_LOCK_AND_HELPER_PLAN.md`

## Requirement Status

- Verify whether `_failure_epoch_reset_conditions` already documents the
  Postgres-only JSON predicate: Complete. The function already carries the
  Postgres caveat comment, so no code change was needed for that item.
- Ensure the already-failed cleanup failure path records
  `workspace.secondary_failure_recorded` with version/event ordering derived
  from a row locked for update: Complete. `destroy_workspace()` now refreshes
  the workspace with `with_for_update=True` after cleanup and before
  status-dependent event handling, and the existing already-failed cleanup test
  now asserts the secondary event order is greater than the failed event order.
- Update worker failure-causality tests so primary failure evidence is seeded by
  a real failed transition before secondary recovery paths are exercised:
  Complete. `_seed_primary_failure_evidence` now uses
  `WorkspaceRepository.transition(..., to=failed)` instead of manually adding a
  state event and overwriting `new_state`.
- Preserve existing regression assertions for primary failure reason, message,
  validation provenance, and secondary failure payloads: Complete. The focused
  worker causality scenarios all passed unchanged.
- Validate with narrow affected tests and static checks: Complete.

## Evidence

Files changed:

- `src/awf/service/controls.py`
- `tests/unit/control/test_worker.py`
- `tests/unit/service/test_controls.py`
- `plans/REVIEW_4445667428_LOCK_AND_HELPER_PLAN.md`
- `plans/REVIEW_4445667428_LOCK_AND_HELPER_VALIDATION.md`

Commands run:

- `python -m compileall -q src/awf/service/controls.py tests/unit/control/test_worker.py tests/unit/service/test_controls.py`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed -q`
  passed: 1 test.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "active_execution_preservation_after_restart_keeps_primary_failure_evidence or stale_active_execution_preserves_validation_failure_and_records_secondary_stale or runtime_stranding_preserves_provider_auth_primary_failure or terminal_runtime_release_failure_preserves_validation_provenance_details"`
  passed: 4 tests, 173 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/control/test_worker.py tests/unit/service/test_controls.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/controls.py`
  passed.

## Gaps

No gaps remain for this review follow-up.
