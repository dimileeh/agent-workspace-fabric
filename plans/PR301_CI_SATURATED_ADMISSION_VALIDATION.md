# PR301 CI Saturated Admission Validation

Plan reference: `plans/PR301_CI_SATURATED_ADMISSION_PLAN.md`

## Requirement Status

- Complete: The provided focused repro nodes now pass.
- Complete: Stale assertions no longer expect requested provisioning while
  execution capacity is saturated.
- Complete: Saturation accounting remains covered: a saturated cycle with no
  execution progress increments `_consecutive_saturated_cycles`.
- Complete: The persistent transient commit regression still proves dispatch is
  prevented when ordered-decision commit fails.
- Complete: Validation evidence uses focused commands only; full AWF/GitHub CI
  remains managed by AWF after agent completion.

## Evidence

Files changed:

- `tests/unit/control/test_worker_parts/test_worker_part_007.py`
- `tests/unit/control/test_worker_parts/test_worker_part_010.py`
- `tests/unit/control/test_worker_parts/test_worker_part_012.py`
- `plans/PR301_CI_SATURATED_ADMISSION_PLAN.md`
- `plans/PR301_CI_SATURATED_ADMISSION_VALIDATION.md`

Commands run:

- Initial repro: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_010.py::TestRunOnceExecutionPart003::test_ready_execution_does_not_block_future_poll_batches tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_persistent_transient_commit_failure_prevents_dispatch tests/unit/control/test_worker_parts/test_worker_part_012.py::TestRunOnceExecutionPart005::test_provisioning_dispatch_does_not_mask_execution_saturation -q` failed with the three CI-reported failures.
- Fixed repro: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_010.py::TestRunOnceExecutionPart003::test_ready_execution_does_not_block_future_poll_batches tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_persistent_transient_commit_failure_prevents_dispatch tests/unit/control/test_worker_parts/test_worker_part_012.py::TestRunOnceExecutionPart005::test_provisioning_dispatch_does_not_mask_execution_saturation -q` passed: `3 passed`.
- Adjacent admission coverage: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q` passed: `15 passed`.
- Focused lint: `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_worker_parts/test_worker_part_007.py tests/unit/control/test_worker_parts/test_worker_part_010.py tests/unit/control/test_worker_parts/test_worker_part_012.py` passed.

## Result

All planned requirements are complete. The local fix keeps the PR #301
saturated-admission behavior intact and updates stale regressions to the
current contract. Full coverage and CI-equivalent validation were not run
locally because AWF/GitHub own those broad gates after this agent phase.
