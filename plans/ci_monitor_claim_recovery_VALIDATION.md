# CI Monitor Claim Recovery Validation

Plan reference: `plans/ci_monitor_claim_recovery_PLAN.md`

## Requirement Status

- Preserve the AWF workspace contract: Complete. No branch switch, push, rebase,
  broad suite, full coverage gate, or CI-equivalent validation was run.
- Keep changes scoped to worker/repository claim handling: Complete. Source
  changes are limited to monitor stranding cleanup, execution-claim release, and
  provisioning transition claim cleanup.
- Treat the CI failure as a real behavior bug: Complete. The fix preserves
  execution claim provenance for monitor recovery and removes the extra
  provisioning release commit by clearing the lease during transition.
- Add or update regression coverage if needed: Complete. Existing failing CI
  tests covered the monitor-recovery regressions and ordered-decision retry.
  Neighboring existing tests covered stale monitor recovery and provisioning
  admission behavior, so no new test was required.
- Record focused verification evidence: Complete.

## Evidence

Changed files:

- `src/awf/control/worker/claims.py`
- `src/awf/control/worker/recovery_stale.py`
- `src/awf/db/repositories/workspace_repo.py`
- `plans/ci_monitor_claim_recovery_PLAN.md`
- `plans/ci_monitor_claim_recovery_VALIDATION.md`

Focused failing-first repro:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_015.py::TestRunOnceMonitorRecoveryPart003::test_repeated_restart_recovery_preserves_active_monitor_claim_idempotently tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_ambiguous_commit_retries_without_duplicate tests/unit/control/test_worker_parts/test_worker_part_014.py::TestRunOnceMonitorRecoveryPart002::test_restart_recovery_clears_stale_execution_claim_and_records_monitor_claim_acquisition tests/unit/control/test_worker_parts/test_worker_part_014.py::TestRunOnceMonitorRecoveryPart002::test_restart_recovery_preserves_unexpired_execution_claim_but_reports_it -q
```

Result before fix: failed, 4 failures.

Focused verification after fix:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_015.py::TestRunOnceMonitorRecoveryPart003::test_repeated_restart_recovery_preserves_active_monitor_claim_idempotently tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_ambiguous_commit_retries_without_duplicate tests/unit/control/test_worker_parts/test_worker_part_014.py::TestRunOnceMonitorRecoveryPart002::test_restart_recovery_clears_stale_execution_claim_and_records_monitor_claim_acquisition tests/unit/control/test_worker_parts/test_worker_part_014.py::TestRunOnceMonitorRecoveryPart002::test_restart_recovery_preserves_unexpired_execution_claim_but_reports_it -q
```

Result: passed, 4 tests.

Neighboring provisioning admission verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_live_named_provisioning_claim_is_hidden_from_sibling_stale_scan tests/unit/control/test_worker_scheduler_admission.py::test_live_named_capacity_provisioning_claim_is_hidden_from_sibling_stale_scan tests/unit/control/test_worker_scheduler_admission.py::test_default_worker_recovers_local_node_provisioning_rows_that_block_admission tests/unit/control/test_worker_scheduler_admission.py::test_named_worker_recovers_null_node_provisioning_rows_that_block_admission -q
```

Result: passed, 4 tests.

Neighboring monitor-recovery verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_037.py::TestRunOnceStaleActiveExecutionRecoveryPart022::test_monitoring_pr_with_open_pr_records_recoverable_runtime_stranding tests/unit/control/test_worker_parts/test_worker_part_037.py::TestRunOnceStaleActiveExecutionRecoveryPart022::test_monitoring_pr_runtime_stranding_clears_expired_claim_and_resumes tests/unit/control/test_worker_parts/test_worker_part_037.py::TestRunOnceStaleActiveExecutionRecoveryPart022::test_monitoring_pr_running_runtime_after_restart_remonitors_open_pr tests/unit/control/test_worker_parts/test_worker_part_015.py::TestRunOnceMonitorRecoveryPart003::test_runtime_stranded_monitoring_pr_with_open_pr_records_and_resumes -q
```

Result: passed, 4 tests.

Focused lint/type checks:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/worker/claims.py src/awf/control/worker/recovery_stale.py src/awf/db/repositories/workspace_repo.py
uv run --python 3.12 --extra dev mypy src/awf/control/worker/claims.py src/awf/control/worker/recovery_stale.py src/awf/db/repositories/workspace_repo.py
```

Result: both passed.

Full AWF/GitHub validation and coverage gates are intentionally left to AWF
after agent completion per the workspace contract.
