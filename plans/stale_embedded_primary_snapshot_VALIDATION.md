# Stale Embedded Primary Snapshot Validation

Plan reference: `plans/stale_embedded_primary_snapshot_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving a cleared/resumed workspace does not bootstrap a primary snapshot from stale embedded event payload alone.
- Complete: Embedded `primary_failure` still enriches snapshots when live workspace failure evidence exists; the existing row-mutation and preserved-primary tests remain green.
- Complete: Validation-run attachment behavior for live validation failures remains covered by the existing focused test file.
- Complete: Ran the focused `failure_causality` test target.

## Evidence

Files changed:

- `src/awf/service/failure_causality.py`
- `tests/unit/service/test_failure_causality.py`
- `plans/stale_embedded_primary_snapshot_PLAN.md`
- `plans/stale_embedded_primary_snapshot_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_primary_failure_snapshot_ignores_stale_embedded_primary_after_resume -q`
  - Failed before implementation with a snapshot populated from stale `primary_failure`.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  - Passed: 8 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  - Passed.
