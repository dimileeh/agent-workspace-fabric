# Review 4445667428 Validation

Plan reference: `plans/REVIEW_4445667428_PLAN.md`

## Requirement Status

- Complete: Keep current-branch AWF workflow intact; no branch switch or push
  was performed.
- Complete: Updated the causality regression test before the production change.
  The targeted test failed against the old implementation because `extra`
  secondary data was included in accumulated history.
- Complete: `build_preserved_failure_payload` now accumulates secondary
  failures only from explicit `previous_secondary_failures` plus the current
  secondary failure.
- Complete: Explicit prior secondary failure accumulation remains covered by
  `test_preserved_failure_payload_accumulates_prior_secondary_failures`.
- Complete: Added a concise comment clarifying that active runtime preservation
  attaches primary failure data as diagnostic refresh operation payload data,
  while durable causality lookup reads failed state-change events.
- Complete: Relevant narrow tests and targeted Ruff passed.

## Evidence

Files changed:

- `src/awf/service/failure_causality.py`
- `src/awf/control/worker.py`
- `tests/unit/service/test_failure_causality.py`
- `plans/REVIEW_4445667428_PLAN.md`
- `plans/REVIEW_4445667428_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_preserved_failure_payload_ignores_secondary_history_in_extra_payload -q`
  failed before the production change, proving the new regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passed: 13 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_preserves_existing_validation_failure -q`
  passed: 1 test.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_active_execution_preservation_after_restart_keeps_primary_failure_evidence -q`
  passed: 1 test.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py src/awf/control/worker.py tests/unit/service/test_failure_causality.py`
  passed.

## Gaps

None.
