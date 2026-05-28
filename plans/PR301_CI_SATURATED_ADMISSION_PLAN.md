# PR301 CI Saturated Admission Plan

## Problem Statement

PR #301 CI fails in worker unit tests after the scheduler admission change that
caps requested provisioning when execution capacity is saturated. The failing
tests still expect provisioning to proceed while execution slots are occupied,
and one monkeypatched `_claim_requested_ids` helper no longer accepts the
current `limit` keyword used by `run_once`.

## Scope

- Keep the scheduler admission fix intact: a worker with an executor must not
  provision additional requested workspaces when execution admission capacity is
  already exhausted.
- Update stale unit-test expectations and names/comments to reflect that
  saturated execution capacity leaves requested work queued.
- Update the mocked claim helper in the persistent transient commit regression
  so it preserves the intended commit-failure assertion with the current helper
  signature.
- Avoid broad validation; AWF/GitHub own full coverage and CI gates after the
  agent phase.

## Requirements Checklist

- The provided focused repro nodes pass.
- The stale tests no longer assert provisioning dispatch while execution slots
  are saturated.
- Saturation accounting remains covered: provisioning must not mask execution
  saturation when no execution progress occurs.
- The persistent transient commit regression still proves dispatch is prevented
  when ordered-decision commit fails.
- Validation evidence records only focused commands.

## Implementation Steps

1. Update `test_ready_execution_does_not_block_future_poll_batches` to assert a
   requested workspace stays queued while the only execution slot is occupied,
   then provisions after the blocking execution releases.
2. Update `test_provisioning_dispatch_does_not_mask_execution_saturation` to
   assert no provisioning dispatch occurs and saturation still ticks.
3. Update the `_claim_without_commit` test double in
   `test_requested_ordered_decision_persistent_transient_commit_failure_prevents_dispatch`
   to accept the `limit` keyword and assert the expected value.
4. Run the focused repro command from the CI evidence, plus the adjacent
   scheduler admission test file if needed for confidence.
5. Save validation results in `plans/PR301_CI_SATURATED_ADMISSION_VALIDATION.md`
   and commit locally with a conventional CI-fix message.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_010.py::TestRunOnceExecutionPart003::test_ready_execution_does_not_block_future_poll_batches tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_persistent_transient_commit_failure_prevents_dispatch tests/unit/control/test_worker_parts/test_worker_part_012.py::TestRunOnceExecutionPart005::test_provisioning_dispatch_does_not_mask_execution_saturation -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`

## Pass Criteria

- The provided failing pytest nodes pass.
- The adjacent scheduler admission tests pass without weakening the admission
  behavior.
- Full AWF/GitHub validation remains deferred to AWF after this agent cycle.
