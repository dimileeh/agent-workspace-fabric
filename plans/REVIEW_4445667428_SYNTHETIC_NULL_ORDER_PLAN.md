# Review 4445667428 Synthetic Null Order Plan

## Problem Statement And Scope

Address the current review-level comment `issue:4445667428` for two narrow
failure-causality follow-ups:

- The already-failed cleanup path writes a synthetic `workspace.state_changed`
  event with `old_state == new_state == failed`; its payload should make that
  synthetic intent machine-readable.
- `_event_occurs_after_or_at_same_tick` still treats an unordered reference
  event as `occurred_at >= reference.occurred_at`, allowing same-timestamp
  epoch reset rows to suppress otherwise valid primary failure evidence.

No branch changes, pushes, rebases, or GitHub comments are in scope.

## Requirements Checklist

- Keep the AWF current-branch workflow intact; do not switch branches or push.
- Add or update regression tests before production changes.
- Mark the already-failed cleanup synthetic state-change payload with an
  explicit machine-readable key.
- Ensure same-timestamp epoch reset rows do not invalidate an unordered
  reference failure event.
- Preserve ordered same-tick epoch reset behavior.
- Run focused validation for the touched controls and failure-causality code.
- Commit local changes with a conventional commit referencing review comment
  `4445667428`.
- Emit the required `AWF-VERDICT` line when complete.

## Implementation Steps

1. Update existing controls coverage to assert the synthetic cleanup event
   carries an explicit marker.
2. Add failure-causality regression coverage for an unordered failed event and
   same-timestamp reset row.
3. Run the targeted tests before implementation and confirm they fail.
4. Add the synthetic payload marker in the already-failed cleanup path.
5. Tighten unordered reference event comparison to use strict timestamp
   ordering for resets after the reference.
6. Re-run focused tests and lint for touched files.
7. Record validation evidence in
   `plans/REVIEW_4445667428_SYNTHETIC_NULL_ORDER_VALIDATION.md`.

## Verification Commands And Pass Criteria

- Targeted new/updated regression tests fail before implementation and pass
  after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed tests/unit/service/test_failure_causality.py::test_epoch_reset_detection_ignores_same_tick_reset_when_reference_event_is_unordered -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py src/awf/service/failure_causality.py tests/unit/service/test_controls.py tests/unit/service/test_failure_causality.py`
  passes.
