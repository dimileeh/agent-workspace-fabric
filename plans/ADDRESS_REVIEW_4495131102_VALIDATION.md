# Address Review Comment 4495131102 Validation

Plan reference: `plans/ADDRESS_REVIEW_4495131102_PLAN.md`

## Requirement Status

- Add a regression test for the capacity-claim race where pre-lock requested IDs
  are already claimed before the capacity-locked scan runs: Complete.
  Evidence: `tests/unit/control/test_worker.py` adds
  `test_capacity_requested_race_does_not_log_prelock_stale_dispatch`.
- Ensure that race does not emit `worker.skip_stale_dispatch` for IDs the
  capacity path did not actually try to transition: Complete.
  Evidence: `src/awf/control/worker.py` no longer logs stale dispatch merely
  because the capacity-locked scan finds no requested candidates.
- Preserve real stale logging when `transition_if_current` loses a race for a
  concrete capacity candidate: Complete.
  Evidence: `_claim_requested_capacity_candidates` still calls
  `_log_stale_requested_claims` when an actual candidate transition returns
  `None`; targeted existing lost-claim tests pass.
- Verify whether metrics allocation summaries are already node-scoped: Complete.
  Evidence: `src/awf/service/metrics.py` passes `node_id` into
  `_allocated_resources_for_session`, which passes it through both persisted
  totals and defaulted DinD-slot totals. Existing
  `test_resource_saturation_scopes_capacity_view_to_local_node` passes.
- Run focused tests for touched behavior: Complete.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_capacity_requested_race_does_not_log_prelock_stale_dispatch -q`
  - Initial run before implementation failed as expected with a captured
    `worker.skip_stale_dispatch` event.
  - Final run passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_race_skip_does_not_record_ordered_decision tests/unit/control/test_worker.py::TestRunOnce::test_capacity_requested_race_does_not_log_prelock_stale_dispatch tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_does_not_record_defaulted_ordered_decision_for_lost_claim tests/unit/control/test_worker.py::TestRunOnce::test_concurrent_capacity_claims_do_not_oversubscribe_requested_workspaces tests/unit/service/test_metrics.py::test_resource_saturation_scopes_capacity_view_to_local_node tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure -q`
  - Passed: 6 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py plans/ADDRESS_REVIEW_4495131102_PLAN.md`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed after formatting `tests/unit/control/test_worker.py`.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

No remaining planned gaps.
