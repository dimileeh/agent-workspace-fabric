# PR270 Review-Level Capacity Defaults Validation

Plan reference: `plans/PR270_REVIEW_LEVEL_CAPACITY_DEFAULTS_PLAN.md`

## Requirement Status

- Complete: `active_latest_totals(node_id=...)` now ranks latest active reservations per workspace before applying the node filter.
- Complete: Global and status-filtered latest reservation behavior remains covered by the existing scheduler-records tests, plus the new node-after-rank regression.
- Complete: Resource metrics now derive missing-row DinD defaults from `Workspace.resolved_profile`.
- Complete: Allocated, planned, and blocked queue metrics include default DinD slots for unreserved DinD-profile workspaces.
- Complete: `LOCAL_CAPACITY_RESERVATION_DEFAULTED` is recorded only after a successful `requested -> provisioning` transition.
- Complete: Focused regressions were added before implementation and confirmed failing before the fix.
- Complete: Changes are limited to the reviewed repository, metrics, and worker behavior plus tests.

## Evidence

Files changed:

- `src/awf/db/repositories.py`
- `src/awf/service/metrics.py`
- `src/awf/control/worker.py`
- `tests/unit/db/test_scheduler_records.py`
- `tests/unit/service/test_metrics.py`
- `tests/unit/control/test_worker.py`

Regression tests added:

- `test_resource_reservation_active_latest_totals_filters_node_after_latest_rank`
- `test_resource_saturation_defaulted_dind_profiles_are_counted_everywhere`
- `test_requested_capacity_gate_does_not_record_defaulted_ordered_decision_for_lost_claim`

Validation commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py::test_resource_reservation_active_latest_totals_filters_node_after_latest_rank tests/unit/service/test_metrics.py::test_resource_saturation_defaulted_dind_profiles_are_counted_everywhere tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_does_not_record_defaulted_ordered_decision_for_lost_claim -q`
  - Before implementation: failed on all three regressions.
  - After implementation: `3 passed in 3.90s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py tests/unit/service/test_metrics.py tests/unit/control/test_worker.py -q`
  - `288 passed in 259.96s`.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Initial run found one explicit `return None` in the new test double.
  - After cleanup: `All checks passed!`.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - `Success: no issues found in 157 source files`.

## Remaining Gaps

None.
