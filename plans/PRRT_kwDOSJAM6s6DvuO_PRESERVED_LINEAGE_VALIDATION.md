# PRRT_kwDOSJAM6s6DvuO Preserved Lineage Validation

## Plan Reference

- `plans/PRRT_kwDOSJAM6s6DvuO_PRESERVED_LINEAGE_PLAN.md`

## Requirement Status

- Add a regression test for expired failed branch lookup plus missing task
  attempt lineage when the preserved worktree contains committed work:
  Complete. Added
  `test_preserved_active_pushed_branch_pr_lookup_failure_with_missing_lineage_keeps_committed_work_operator_recoverable`.
- Preserve during-grace blocking behavior:
  Complete. Existing grace-period regression still passes.
- Do not request automatic validation without `attempt_id` and `task_id`:
  Complete. Missing-lineage committed work now records operator-required
  recovery and the regression asserts no validation request is created.
- After grace, classify the worktree before declaring salvage unrecoverable for
  failed branch lookup:
  Complete. The new regression proves committed work is classified after the
  failed lookup expires.
- Record `OPERATOR_REQUIRED` with classification and branch lookup failure
  payload when committed work lacks lineage:
  Complete. The new regression asserts committed classification details and the
  branch lookup failure payload are present on the operator-required event.

## Evidence

- Changed `src/awf/control/worker.py`.
- Changed `tests/unit/control/test_worker.py`.
- Added plan and validation files for the required plan-and-validate workflow.

## Verification

- Before the worker fix, the new focused regression failed because the workspace
  remained in `runtime_preserved_salvage_blocked` and emitted stale-active
  failure evidence instead of operator-required recovery.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_missing_lineage_keeps_committed_work_operator_recoverable -q`
  passed after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_retries_during_grace tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_validates_committed_work tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_missing_lineage_keeps_committed_work_operator_recoverable tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_no_local_work_replaces tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_missing_attempt_lineage_records_audit_before_grace tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_missing_lineage_audit_does_not_block_expired_failure -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  passed.

## Gaps

- None.
