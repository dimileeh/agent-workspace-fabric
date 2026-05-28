# Scheduler Ready Queue Capacity Validation

Plan reference: `plans/SCHEDULER_READY_QUEUE_CAPACITY_PLAN.md`

## Requirement Status

- Complete: Requested admission is capped by `max_concurrent_provisions`, in-process execution task availability, and active workspace rows for the worker node.
- Complete: A saturated worker leaves new requested workspaces in `requested` and does not invoke provisioning.
- Complete: The cap applies to ordinary requested claims and the local-capacity scheduler path.
- Complete: Healthy `ready` workspaces with running runtimes are no longer marked as stale lost executions while waiting for execution.
- Complete: Broken ready runtimes still flow through existing runtime health classification because only `finding is None` is skipped.
- Complete: Focused regression tests, neighboring worker tests, ruff, and mypy passed.

## Evidence

Files changed:

- `src/awf/control/worker/manager.py`
- `src/awf/control/worker/claims.py`
- `src/awf/control/worker/constants.py`
- `src/awf/control/worker/recovery_stale.py`
- `tests/unit/control/test_worker_scheduler_admission.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_002.py::TestRunOncePart002::test_claim_requested_ids_short_circuits_without_database tests/unit/control/test_worker_parts/test_worker_part_006.py::TestRunOncePart006::test_capacity_requested_path_skips_prelock_status_filter tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_ambiguous_commit_retries_without_duplicate tests/unit/control/test_worker_parts/test_worker_part_015.py::TestRunOnceMonitorRecoveryPart003::test_monitor_resume_and_ready_execution_share_execution_limit tests/unit/control/test_worker_parts/test_worker_part_037.py::TestRunOnceStaleActiveExecutionRecoveryPart022::test_monitoring_pr_without_pr_url_follows_failure_path tests/unit/control/test_worker_parts/test_worker_part_037.py::TestRunOnceStaleActiveExecutionRecoveryPart022::test_terminal_rows_are_ignored -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker tests/unit/control/test_worker_scheduler_admission.py`
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker`
- `git diff --check`

## Result

All planned requirements are satisfied. The failed workspace timeline was reproduced in tests: over-admission under saturated execution slots and stale misclassification of healthy ready runtimes both fail on the old behavior and pass with the fix.
