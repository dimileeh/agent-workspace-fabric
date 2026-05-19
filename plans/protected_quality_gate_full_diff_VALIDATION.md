# Protected Quality Gate Full Diff Validation

Plan reference: `plans/protected_quality_gate_full_diff_PLAN.md`

## Requirement Status

- Regression for self-committed protected edit before staged work: Complete.
  Added
  `tests/unit/control/test_executor_validation_fix_cycle.py::TestProtectedQualityGateChanges::test_initial_agent_self_committed_protected_change_before_staged_work_is_blocked`.
  Confirmed it failed before implementation because the workspace completed and
  opened a PR instead of failing policy.
- Preserve `owned_paths` behavior: Complete. The new committed-output helper
  passes `owned_paths=list(ws.owned_paths)` through
  `find_protected_quality_gate_changes`.
- Re-check committed output before normal executor push: Complete. The executor
  now calls `_fail_if_protected_quality_gate_committed_output` before the
  `validating -> pushing` transition.
- Fail with `QUALITY_GATE_POLICY_CHANGED`: Complete. The helper marks the
  workspace failed with `FailureReason.policy_failure` and the existing reason
  code/message.
- Keep changes minimal: Complete. Production change is a narrow helper plus one
  call site; tests only add the regression and update fake command queues for
  the extra pre-push diff check.

## Evidence

Files changed:

- `src/awf/control/executor.py`
- `tests/unit/control/test_executor_validation_fix_cycle.py`
- `tests/unit/control/test_executor.py`
- `tests/unit/control/test_executor_error_paths.py`
- `tests/unit/control/test_executor_monitor_recovery.py`
- `plans/protected_quality_gate_full_diff_PLAN.md`
- `plans/protected_quality_gate_full_diff_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py::TestProtectedQualityGateChanges::test_initial_agent_self_committed_protected_change_before_staged_work_is_blocked -q`
  - Failed before implementation with completed workspace status.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py::TestProtectedQualityGateChanges -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py::TestHappyPath::test_drives_ready_to_completed_and_records_pr_url tests/unit/control/test_executor.py::TestFailurePaths::test_orphan_history_is_recovered_and_pipeline_continues tests/unit/control/test_executor_monitor_recovery.py::test_rebase_only_recovery_with_conformance_handoff_pushes_report_commit -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestBranchDriftRecovery::test_drift_with_clean_worktree_is_recovered tests/unit/control/test_executor_error_paths.py::TestBranchDriftRecovery::test_drift_with_uncommitted_wip_preserves_it tests/unit/control/test_executor_error_paths.py::TestPrMonitorFactoryPath::test_factory_builds_monitor_once_and_it_runs tests/unit/control/test_executor_error_paths.py::TestPrMonitorFactoryPath::test_existing_pr_recovery_pushes_and_resumes_monitor_without_duplicate_create -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/control/test_executor.py tests/unit/control/test_executor_monitor_recovery.py tests/unit/control/test_executor_error_paths.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

No planned requirements remain partial or missing.
