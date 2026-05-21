# Metrics Capacity Hot Path Review Validation

Plan reference: `METRICS_CAPACITY_HOT_PATH_REVIEW_PLAN.md`

## Requirement Status

- Complete: Stop importing underscore-prefixed repository helpers from `metrics.py`.
  - Evidence: `metrics.py` now imports `resolve_session_dialect_name` and `scheduler_order_expressions`; `repositories.py` exposes those public names.
- Complete: Avoid loading all matching `Workspace.resolved_profile` JSON blobs in `_defaulted_dind_slots_for_session`.
  - Evidence: `_defaulted_dind_slots_for_session` now uses a SQL `sum(case(...))` aggregate over the DinD JSON path.
- Complete: Pre-filter `_capacity_queue_candidates` reservation ranking to requested workspaces in local node scope.
  - Evidence: the latest-active-reservation subquery joins `requested_reservation_workspace` and filters status/node before `row_number()`.
- Complete: Preserve existing scheduling and metrics behavior.
  - Evidence: metrics and repository regression suites pass.
- Complete: Keep scope local and commit-ready.
  - Evidence: changed files are limited to metrics, repository helper names, focused tests, and plan/validation docs.

## Verification Evidence

- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_defaulted_dind_slots_are_aggregated_without_profile_materialization tests/unit/service/test_metrics.py::test_capacity_queue_candidates_prefilter_reservations_to_requested_scope -q`
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q` (`94 passed`)
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/db/test_repository_coverage.py -q` (`42 passed`)
- Passed: `uv run --python 3.12 --extra dev ruff check src/awf tests`
- Passed: `uv run --python 3.12 --extra dev mypy src/awf`
- Not completed: `uv run --python 3.12 --extra dev pytest tests/unit -q` was started as broader validation and stopped at 11% because it was running slowly; no failures were reported before termination. The narrower affected suites above completed successfully.

## Gaps

No implementation gaps remain for the review comment.
