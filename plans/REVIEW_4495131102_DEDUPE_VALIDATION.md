# Review 4495131102 Deduplication Validation

Plan reference: `REVIEW_4495131102_DEDUPE_PLAN.md`

## Requirement Status

- Complete: Add regression coverage for the shared DinD default helper.
  - Evidence: `tests/unit/service/test_resource_capacity.py` imports and verifies `default_dind_slots_from_profile`.
- Complete: Move the DinD default-profile heuristic to a shared capacity module and import it from scheduler and metrics code.
  - Evidence: `src/awf/service/resource_capacity.py` owns `default_dind_slots_from_profile`; `src/awf/control/worker.py` and `src/awf/service/metrics.py` import it.
- Complete: Reuse the repository-level empty reservation totals helper from metrics instead of keeping a duplicate implementation.
  - Evidence: `src/awf/db/repositories.py` exposes `empty_resource_reservation_totals`; `src/awf/service/metrics.py` imports it and no longer defines its own copy.
- Complete: Preserve existing resource capacity and scheduler behavior.
  - Evidence: implementation removes duplicate definitions only; the worker default-demand call and metrics default-DinD aggregation keep the same inputs and return values.
- Complete: Run narrow verification that covers the changed helper paths.
  - Evidence: commands below.

## Verification Evidence

- Red test confirmation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py::test_default_dind_slots_from_profile_detects_dind_mode tests/unit/db/test_resource_reservation_totals.py::test_empty_resource_reservation_totals_covers_all_reservation_dimensions -q`
  - Result: failed during collection because `default_dind_slots_from_profile` and `empty_resource_reservation_totals` were not yet importable.
- Targeted tests:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py tests/unit/db/test_resource_reservation_totals.py tests/unit/api/test_metrics_capacity.py::test_active_latest_totals_for_workspace_scope_delegates_to_repository -q`
  - Result: passed, `8 passed`.
- Lint:
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/db/repositories.py src/awf/service/metrics.py src/awf/service/resource_capacity.py tests/unit/service/test_resource_capacity.py tests/unit/db/test_resource_reservation_totals.py`
  - Result: passed.
- Type check:
  - `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed, no issues in 157 source files.

## Notes

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py tests/unit/db/test_resource_reservation_totals.py tests/unit/api/test_metrics_capacity.py -q` was also attempted. It reported one failure in `test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing`. The failed assertion concerns allocated-resource node scoping in unchanged SQL that already exists in `HEAD`, so it is outside this deduplication review comment.
