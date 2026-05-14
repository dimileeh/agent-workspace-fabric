# Cleanup Failure Causality Review Validation

Plan reference: `plans/CLEANUP_FAILURE_CAUSALITY_REVIEW_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing cleanup failure secondary evidence
  reaches a failed `workspace.state_changed` event when cleanup observes an
  already `failed` workspace.
- Complete: Preserved ordinary cleanup failure transition behavior by keeping
  the existing `destroying -> failed` `repo.transition()` path unchanged.
- Complete: Cancelled/completed/destroyed stale cleanup callbacks remain on the
  stale-callback ignored path; only failed cleanup results for already-failed
  workspaces continue into causality recording.
- Complete: Primary failure row fields and failed-event reason codes remain
  rooted in the original primary failure when primary evidence exists.
- Complete: `attach_primary_failure` is now non-destructive when callers already
  supplied `primary_failure`, with regression coverage.
- Complete: Edits are scoped to the cleanup causality path, the helper contract,
  focused tests, and required plan/validation records.

## Evidence

Files changed:

- `src/awf/service/controls.py`
- `src/awf/service/failure_causality.py`
- `tests/unit/service/test_controls.py`
- `tests/unit/service/test_failure_causality.py`
- `plans/CLEANUP_FAILURE_CAUSALITY_REVIEW_PLAN.md`
- `plans/CLEANUP_FAILURE_CAUSALITY_REVIEW_VALIDATION.md`

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed -q`
  - Failed before implementation with `workspace destroy callback ignored`;
    passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_attach_primary_failure_preserves_existing_primary_failure_key -q`
  - Failed before implementation because the helper replaced the existing
    primary failure; passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py tests/unit/service/test_failure_causality.py -q`
  - Passed: 61 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py src/awf/service/failure_causality.py tests/unit/service/test_controls.py tests/unit/service/test_failure_causality.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

None.
