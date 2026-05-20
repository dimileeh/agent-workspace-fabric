# Review 4495131102 Scope Alignment Validation

Plan reference: `plans/REVIEW_4495131102_SCOPE_ALIGNMENT_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving capacity queue blocker counts use
  scheduler allocation scope when a local-node reservation belongs to a
  workspace whose `workspace.node_id` points elsewhere.
  - Evidence:
    `test_capacity_queue_uses_scheduler_allocation_scope_for_migrating_reservation`
    failed before implementation with `{}` blocker counts and passed after the
    scheduler-scope allocation seed was added.
- Complete: Preserved the public allocated resource metrics behavior for this
  migration edge case.
  - Evidence: the new regression asserts
    `summary.allocated_resources.active_workspace_count == 0` while
    `capacity_queue.blocked_reason_counts` reports the scheduler-gated peak CPU
    blocker.
- Complete: Added a regression test proving
  `active_latest_by_workspace_ids` returns only the latest active reservation
  per workspace and ignores newer released reservations.
  - Evidence:
    `test_resource_reservation_active_latest_by_workspace_ids_uses_window_query`
    verifies returned reservation IDs for duplicate workspace input.
- Complete: Replaced Python-side deduplication with the repository-standard SQL
  `ROW_NUMBER() OVER (PARTITION BY workspace_id ...)` pattern.
  - Evidence: the repository regression failed before implementation because
    the captured SQL did not contain `row_number() over`, then passed after the
    ranked subquery rewrite.
- Complete: Ran focused and nearby validation for metrics and repository
  behavior.

## Evidence

Files changed:

- `src/awf/service/metrics.py`
- `src/awf/db/repositories.py`
- `tests/unit/service/test_metrics.py`
- `tests/unit/db/test_scheduler_records.py`
- `plans/REVIEW_4495131102_SCOPE_ALIGNMENT_PLAN.md`
- `plans/REVIEW_4495131102_SCOPE_ALIGNMENT_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "scheduler_allocation_scope_for_migrating_reservation"`
  - Before implementation: failed with empty `blocked_reason_counts`.
  - After implementation: passed, `1 passed, 90 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "active_latest_by_workspace_ids_uses_window_query"`
  - Before implementation: failed because captured SQL did not use
    `row_number() over`.
  - After implementation: passed, `1 passed, 11 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue or allocated_capacity_matches_scheduler_null_node_rules or scopes_capacity_view_to_local_node"`
  - Passed, `10 passed, 81 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q`
  - Passed, `12 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  - Passed, `91 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py src/awf/db/repositories.py tests/unit/service/test_metrics.py tests/unit/db/test_scheduler_records.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `git diff --check`
  - Passed.

## Remaining Gaps

None.
