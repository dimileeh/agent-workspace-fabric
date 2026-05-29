# CI Monitor Claim Recovery Plan

## Problem Statement And Scope

PR #301 CI fails in the focused worker restart/monitor recovery tests. The
failures are scoped to monitor-recovery claim handling and ordered requested
decision retry idempotency:

- stale execution claims must be cleared during monitor recovery and recorded
  as `cleared_stale`;
- unexpired execution claims must be preserved and reported as
  `preserved_unexpired`;
- repeated monitor recovery attempts must not overwrite the original recovery
  operation payload with post-cleanup claim state;
- ambiguous commit retries for requested provisioning must avoid duplicate queue
  decisions.

## Requirements Checklist

- Preserve the AWF workspace contract: no branch switching, pushing, rebasing, or
  broad AWF/GitHub validation from the agent phase.
- Keep changes scoped to the worker/repository paths that drive the failing
  tests.
- Treat the CI failure as a real behavior bug; do not skip or weaken tests.
- Add or update regression coverage only if the existing failing node IDs do not
  already cover the fixed behavior.
- Record focused verification evidence here and in the validation document.

## Implementation Steps

1. Reproduce the four AWF-provided failing pytest node IDs.
2. Inspect the monitor recovery claim flow and ordered requested decision retry
   flow.
3. Fix claim cleanup so monitor recovery records cleanup from the pre-claim
   workspace state while the repository atomically clears only stale execution
   claims.
4. Fix ordered decision retry idempotency if it is a distinct issue after the
   monitor recovery fix.
5. Run the same focused pytest node IDs until they pass.
6. Run a narrow lint/type or targeted neighboring test only if the edited code
   needs additional confidence.
7. Create `plans/ci_monitor_claim_recovery_VALIDATION.md` with requirement
   status and command evidence.

## Assumptions/Changes

- The monitor recovery failures came from recoverable runtime stranding cleanup
  clearing execution-claim state before monitor recovery could record it.
- The ordered decision retry failure was caused by provisioning claim release
  adding a separate post-provision commit. Provisioning transitions now clear
  the provisioning execution lease as part of the state transition, leaving the
  release helper as an idempotent fallback.

## Verification Commands And Pass Criteria

Focused repro and final check:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_015.py::TestRunOnceMonitorRecoveryPart003::test_repeated_restart_recovery_preserves_active_monitor_claim_idempotently tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_ambiguous_commit_retries_without_duplicate tests/unit/control/test_worker_parts/test_worker_part_014.py::TestRunOnceMonitorRecoveryPart002::test_restart_recovery_clears_stale_execution_claim_and_records_monitor_claim_acquisition tests/unit/control/test_worker_parts/test_worker_part_014.py::TestRunOnceMonitorRecoveryPart002::test_restart_recovery_preserves_unexpired_execution_claim_but_reports_it -q
```

Pass criteria: all four tests pass. Full AWF/GitHub validation is intentionally
left to AWF after agent completion per the workspace contract.
