# Active Replacement Attempt Mismatch Validation

Plan reference: `plans/ACTIVE_REPLACEMENT_ATTEMPT_MISMATCH_PLAN.md`

## Requirement Status

- Regression test for silent mismatch guard: Complete.
  - Added `test_preserved_active_replacement_attempt_mismatch_records_not_possible`
    and `test_preserved_active_recovery_propagates_replacement_not_possible` in
    `tests/unit/control/test_worker.py`.
- Preserve idempotency guard and avoid replacement on stale lineage: Complete.
  - `_create_preserved_active_replacement` still returns before replacement creation when
    the current source attempt is missing or has a different id.
- Record explicit salvage outcome and log entry: Complete.
  - The mismatch path now logs `worker.preserved_active_replacement_attempt_mismatch`
    and records `workspace.active_execution_salvage_not_possible`.
- Let stale-active failure proceed after unrecoverable mismatch: Complete.
  - `_create_preserved_active_replacement` returns `False` for the mismatch path, and
    `_recover_preserved_active_execution` returns that value to the stale-active caller.
- Preserve existing replacement behavior: Complete.
  - The happy path and existing missing replacement-attempt warning path still return
    successful recovery handling.

## Evidence

- Changed files:
  - `src/awf/control/worker.py`
  - `tests/unit/control/test_worker.py`
  - `plans/ACTIVE_REPLACEMENT_ATTEMPT_MISMATCH_PLAN.md`
  - `plans/ACTIVE_REPLACEMENT_ATTEMPT_MISMATCH_VALIDATION.md`
- Confirmed initial regression failure:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_replacement_attempt_mismatch_records_not_possible -q`
  - Failed before implementation with `assert None is False`.
- Passing verification:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_replacement_attempt_mismatch_records_not_possible -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_recovery_propagates_replacement_not_possible -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "replacement and attempt"`
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q` (`286 passed`)
  - `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - `uv run --python 3.12 --extra dev mypy src/awf`
