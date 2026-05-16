# Failure Causality Remonitor Epoch Validation

Plan reference: `plans/FAILURE_CAUSALITY_REMONITOR_EPOCH_PLAN.md`

## Requirement Status

- Add a regression test for remonitor reset followed by a different current
  failure reason: Complete. Added
  `test_primary_failure_snapshot_uses_current_failure_after_remonitor_reset`.
- Preserve cleanup behavior without a remonitor/retry epoch reset: Complete.
  Existing cleanup preservation tests still pass.
- Ignore embedded primary payloads from failed events before a failure epoch
  reset: Complete. `_primary_failure_event_for_current_epoch()` now checks for
  later active/remonitor reset states before reusing embedded primary evidence.
- Let current row/latest failed event supply evidence after reset: Complete.
  The new regression asserts `agent_failure`, current message, and current
  reason code are used, with no stale validation run attached.
- Keep changes narrow and avoid GitHub writes or branch changes: Complete.
  Only local files were changed.

## Evidence

- Changed `src/awf/service/failure_causality.py`.
- Changed `tests/unit/service/test_failure_causality.py`.
- Added this plan/validation pair under `plans/`.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passed with 9 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/failure_causality.py`
  passed.

No remaining planned gaps.
