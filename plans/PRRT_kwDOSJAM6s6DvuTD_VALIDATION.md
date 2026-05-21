# PRRT_kwDOSJAM6s6DvuTD Validation

Plan reference: `PRRT_kwDOSJAM6s6DvuTD_PLAN.md`

## Requirement Status

- Regression test for a current validation-requested salvage event with no
  terminal decision: Complete. Added
  `test_preserved_active_validation_request_without_terminal_decision_blocks_stale_failure`.
- Existing stale-failure behavior after formal unrecoverable validation recovery:
  Complete. Updated the slot-disabled regression to require
  `ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE` before stale failure remains allowed.
- Current preservation cycle and workspace-status scoping: Complete.
  `_stale_active_execution_can_fail` checks both validation-requested and
  not-possible events through `_has_current_salvage_event` using the latest
  preservation timestamp and candidate status.
- Existing stale-active recovery regressions preserved: Complete. The prior
  slot-disabled expectation still passes and now has stronger provenance.
- Verification: Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PRRT_kwDOSJAM6s6DvuTD_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DvuTD_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_validation_request_without_terminal_decision_blocks_stale_failure -q`
  - Failed before the fix as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_validation_slot_exhaustion_after_grace_does_not_block_stale_failure tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_validation_request_without_terminal_decision_blocks_stale_failure -q`
  - Passed: 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_validation"`
  - Passed: 9 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py -q -k "stale_active_execution_can_fail"`
  - Passed: 3 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Passed: 287 tests.
- `uv run --python 3.12 --extra dev ruff format src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Reformatted `src/awf/control/worker.py`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed after formatting.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_validation_slot_exhaustion_after_grace_does_not_block_stale_failure tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_validation_request_without_terminal_decision_blocks_stale_failure -q`
  - Passed after formatting: 2 tests.

## Gaps

None.
