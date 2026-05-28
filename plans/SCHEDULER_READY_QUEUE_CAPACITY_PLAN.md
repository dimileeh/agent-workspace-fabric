# Scheduler Ready Queue Capacity Plan

## Problem Statement

Workspace `ws_e2037f21b84c4246a9514c58` was admitted while five execution slots were already occupied. It moved from `requested` to `provisioning` and `ready`, then failed as `STALE_ACTIVE_EXECUTION` because a healthy ready runtime had no execution task. A saturated AWF worker should leave the next workspace queued in `requested` until an execution slot is available.

## Scope

- Fix control-worker admission so requested workspaces are not provisioned beyond available execution slots when an executor is configured.
- Preserve existing provision-only behavior for worker deployments without an executor.
- Ensure healthy `ready` workspaces waiting for an execution slot are not treated as stale lost executions.
- Add focused regression tests for saturated-slot requested admission and ready runtime health classification.

## Requirements Checklist

- Requested admission must be capped by available execution slots as well as `max_concurrent_provisions`.
- Requested admission must also count active workspace rows on the worker node, so restart or cross-worker visibility does not over-admit when in-process task tracking is empty or incomplete.
- When all execution slots are occupied, a new requested workspace must remain `requested` and the provisioner must not run.
- The cap must apply to both ordinary and local-capacity scheduling paths.
- A healthy `ready` workspace with a running runtime must be ignored by stale active execution recovery instead of being marked/failing as stale.
- Broken ready runtimes should still be classified by existing runtime health checks.
- Validation must use targeted pytest plus ruff/mypy on touched Python files.

## Implementation Steps

1. Add a worker helper that computes requested-provision admission slots from `max_concurrent_provisions`, current in-process execution slot availability, and active DB workspace rows for the worker node.
2. Use that helper in `run_once` before listing/claiming requested workspaces.
3. Thread an optional claim limit through the local-capacity requested-claim path.
4. Short-circuit stale active execution recovery for healthy `ready` candidates.
5. Add focused unit tests covering saturated execution slots and healthy ready queue behavior.
6. Run targeted tests and lint/type checks.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_002.py::TestRunOncePart002::test_claim_requested_ids_short_circuits_without_database tests/unit/control/test_worker_parts/test_worker_part_006.py::TestRunOncePart006::test_capacity_requested_path_skips_prelock_status_filter tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_ambiguous_commit_retries_without_duplicate tests/unit/control/test_worker_parts/test_worker_part_015.py::TestRunOnceMonitorRecoveryPart003::test_monitor_resume_and_ready_execution_share_execution_limit tests/unit/control/test_worker_parts/test_worker_part_037.py::TestRunOnceStaleActiveExecutionRecoveryPart022::test_monitoring_pr_without_pr_url_follows_failure_path tests/unit/control/test_worker_parts/test_worker_part_037.py::TestRunOnceStaleActiveExecutionRecoveryPart022::test_terminal_rows_are_ignored -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker tests/unit/control/test_worker_scheduler_admission.py`
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker`

## Pass Criteria

- Regression tests fail on the original behavior and pass after the fix.
- Existing focused worker tests still pass.
- Ruff and mypy pass on touched control-worker code.
