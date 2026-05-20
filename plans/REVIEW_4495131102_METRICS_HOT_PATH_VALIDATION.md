# Review 4495131102 Metrics Hot Path Validation

Plan reference: `plans/REVIEW_4495131102_METRICS_HOT_PATH_PLAN.md`

## Requirement Status

- Complete: Added regression coverage requiring the capacity-queue blocker
  candidate SQL to include a `LIMIT`.
- Complete: Preserved existing blocker-count behavior for queues smaller than
  the bound; the focused `capacity_queue_blocked_reason_counts` tests pass.
- Complete: Kept `queued_workspace_count`, `oldest_workspace_id`, and planned
  queue resource totals as whole-queue summaries. Only the blocker candidate
  FIFO diagnostic query is bounded.
- Complete: Preserved the queue-specific API/console contract where planned
  queue resources omit `active_workspace_count`; the OpenAPI contract test and
  console typecheck pass.
- Complete: Ran focused service/API/console validation and static checks.

## Evidence

Files changed:

- `src/awf/service/metrics.py`
- `tests/unit/service/test_metrics.py`
- `plans/REVIEW_4495131102_METRICS_HOT_PATH_PLAN.md`
- `plans/REVIEW_4495131102_METRICS_HOT_PATH_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_blocked_reason_counts_loads_latest_requested_demands_once -q`
  - Failed before implementation because the candidate query had no `LIMIT`.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_blocked_reason_counts"`
  - Passed: `7 passed, 84 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_capacity_queue_planned_resources_uses_queue_specific_schema -q`
  - Passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py tests/unit/api/test_openapi_artifact.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/metrics.py`
  - Passed.
- `npm --prefix apps/console run typecheck`
  - Passed.

## Remaining Gaps

None.
