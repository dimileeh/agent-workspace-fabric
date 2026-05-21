# PRRT_kwDOSJAM6s6DxuzC Open PR Lookup Failure Validation

## Plan Reference

- `plans/PRRT_kwDOSJAM6s6DxuzC_OPEN_PR_LOOKUP_FAILURE_PLAN.md`

## Requirement Status

- Preserve automatic replacement when open PR lookup succeeds and confirms no
  open PR for a clean no-work branch: Complete. The replacement regression now
  uses a successful empty resolver result.
- Treat failed open PR lookup as operator-recoverable ambiguity after grace:
  Complete. Added regressions for committed work and no local work; both record
  `ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED` with classification evidence.
- Preserve during-grace lookup failure blocking and retry behavior: Complete.
  Existing grace-period regression still passes.
- Keep failed lookup payload evidence on the recovery event: Complete. New
  operator-required regressions assert the `branch_pr_lookup` failure payload.

## Evidence

- Changed `src/awf/control/worker.py`.
- Changed `tests/unit/control/test_worker.py`.
- Added plan and validation files for this review-thread fix.

## Verification

- Before the worker fix, the new failed-lookup committed-work and no-work
  regressions failed because the workspace entered automatic validation or
  replacement instead of operator recovery.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_retries_during_grace tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_no_open_pr_validates_committed_work tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_committed_work_requires_operator_recovery tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_missing_lineage_keeps_committed_work_operator_recoverable tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_no_open_pr_with_no_local_work_replaces tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_no_local_work_requires_operator_recovery -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  passed.

## Gaps

- None.
