# Failure Causality Review 4445667428 Validation

Plan reference: `FAILURE_CAUSALITY_REVIEW_4445667428_PLAN.md`

## Requirement Status

- Add regression coverage for same-timestamp reset detection: Complete.
  `tests/unit/service/test_failure_causality.py` now covers reset detection with
  equal `occurred_at` values and event-id tiebreaking.
- Add regression coverage preventing old-epoch validation runs from being
  attached to current validation failures: Complete.
  `tests/unit/service/test_failure_causality.py` now covers a remonitor/reset
  followed by a current validation failure before a new validation run finishes.
- Add regression coverage proving cleanup result/audit secondary history comes
  from `build_preserved_failure_payload`: Complete.
  `tests/unit/service/test_controls.py` now verifies operation result and audit
  evidence reuse the helper-built secondary payload.
- Update failure causality queries without changing unrelated scheduler,
  provider, or state-machine behavior: Complete.
  Changes are scoped to `src/awf/service/failure_causality.py`.
- Preserve existing primary/secondary failure payload semantics: Complete.
  Existing causality, controls, and worker tests pass.

## Evidence

Files changed:

- `src/awf/service/failure_causality.py`
- `src/awf/service/controls.py`
- `tests/unit/service/test_failure_causality.py`
- `tests/unit/service/test_controls.py`
- `plans/FAILURE_CAUSALITY_REVIEW_4445667428_PLAN.md`
- `plans/FAILURE_CAUSALITY_REVIEW_4445667428_VALIDATION.md`

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_epoch_reset_detection_uses_same_timestamp_event_id_tiebreaker tests/unit/service/test_failure_causality.py::test_primary_failure_snapshot_filters_validation_runs_before_current_epoch tests/unit/service/test_controls.py::test_destroy_cleanup_failure_preserves_existing_validation_failure -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py tests/unit/service/test_controls.py -q`
  passed: 48 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passed: 176 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

Additional note: `uv run --python 3.12 --extra dev pytest tests/unit -q` was
started for broader signal, showed no visible failures through roughly 10%
completion, and was terminated because it was substantially broader and slower
than the review fix scope.
