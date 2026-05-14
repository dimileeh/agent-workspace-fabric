# Review 4445667428 Batch And Secondary Event Validation

Plan reference:
`plans/REVIEW_4445667428_BATCH_AND_SECONDARY_EVENT_PLAN.md`

## Requirement Status

- Preserve existing single-event `add_event()` behavior: Complete.
  `add_events()` now applies an index offset, so the single-event wrapper still
  writes `event_order == workspace.version`.
- Assign deterministic, increasing `event_order` values within one
  `add_events()` batch: Complete. Added repository coverage for two events in
  one batch receiving adjacent orders.
- Replace the already-failed cleanup synthetic `workspace.state_changed` record
  with an event type that is not a lifecycle transition: Complete. Already
  failed cleanup now emits `workspace.secondary_failure_recorded`.
- Keep failure-causality snapshots able to recover secondary cleanup failures
  from the new event type: Complete. Failure-causality queries now include the
  secondary-failure event type when reading failed causality history.
- Add regression coverage before implementation and confirm focused failures:
  Complete. The new focused tests failed before implementation for duplicated
  batch order, synthetic state-change self-loop, and missing secondary history.
- Run focused repository, controls, failure-causality, callback, lint, and type
  checks: Complete. See evidence below.
- Commit the local fix with a conventional commit referencing the review
  comment id: Complete. This validation file is included in the local
  conventional commit for this review fix.

## Evidence

Files changed:

- `src/awf/db/repositories.py`
- `src/awf/service/controls.py`
- `src/awf/service/failure_causality.py`
- `src/awf/common/callback_events.py`
- `tests/unit/db/test_workspace_repository.py`
- `tests/unit/service/test_controls.py`
- `tests/unit/service/test_failure_causality.py`
- `tests/unit/common/test_callback_events.py`
- `tests/unit/api/test_callbacks.py`
- `plans/REVIEW_4445667428_BATCH_AND_SECONDARY_EVENT_PLAN.md`
- `plans/REVIEW_4445667428_BATCH_AND_SECONDARY_EVENT_VALIDATION.md`

Failing-before command:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestAddEvents::test_batch_assigns_increasing_event_order tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_reads_secondary_failure_recorded_events -q`
  failed as expected after regression tests were added and before
  implementation.

Passing commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestAddEvents::test_batch_assigns_increasing_event_order tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_reads_secondary_failure_recorded_events tests/unit/common/test_callback_events.py tests/unit/api/test_callbacks.py::test_register_callback_accepts_exact_public_event_types -q`
  passed: 6 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py tests/unit/service/test_controls.py tests/unit/service/test_failure_causality.py tests/unit/common/test_callback_events.py tests/unit/api/test_callbacks.py -q`
  passed: 183 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py src/awf/service/controls.py src/awf/service/failure_causality.py src/awf/common/callback_events.py tests/unit/db/test_workspace_repository.py tests/unit/service/test_controls.py tests/unit/service/test_failure_causality.py tests/unit/common/test_callback_events.py tests/unit/api/test_callbacks.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/db/repositories.py src/awf/service/controls.py src/awf/service/failure_causality.py src/awf/common/callback_events.py`
  passed.

## Gaps

None.
