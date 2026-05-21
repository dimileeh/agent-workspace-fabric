# Capacity Queue Batch Prefetch Validation

Plan reference: `plans/CAPACITY_QUEUE_BATCH_PREFETCH_PLAN.md`

## Requirement Status

- Regression test proving capacity-page processing avoids per-workspace
  `TaskAttemptRepository.get_by_workspace_id` and
  `QueueDecisionRepository.list_for_workspace`: Complete.
  Evidence: `tests/unit/control/test_worker.py` adds
  `test_requested_capacity_gate_prefetches_queue_decision_context_for_blocked_page`,
  which patches the single-row methods to fail and asserts one batch attempt
  lookup plus one batch latest-decision lookup for the blocked page.
- Batch-fetch task attempts for all page candidates before the capacity loop:
  Complete.
  Evidence: `src/awf/db/repositories.py` adds
  `TaskAttemptRepository.get_by_workspace_ids`; `src/awf/control/worker.py`
  calls it once for `candidate_ids`.
- Batch-fetch latest queue decisions for all page candidates before the capacity
  loop: Complete.
  Evidence: `src/awf/control/worker.py` calls
  `QueueDecisionRepository.latest_by_workspace_ids(candidate_ids)` once before
  iterating candidates.
- Preserve deferred-decision deduplication, previous resource-summary
  carry-forward, and missing-attempt warning behavior: Complete.
  Evidence: `_record_capacity_queue_decision` still owns deduplication and
  previous-summary handling, accepts prefetched context for the capacity gate,
  and keeps the existing direct-call fallback with the early missing-attempt
  warning return.
- Keep changes scoped to worker scheduling and repository helpers: Complete.
  Evidence: Code changes are limited to `src/awf/control/worker.py`,
  `src/awf/db/repositories.py`, and the focused regression test.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k prefetches_queue_decision_context`
  failed before implementation with `AssertionError: capacity decisions should
  batch-fetch task attempts`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k prefetches_queue_decision_context`
  passed after implementation: `1 passed, 222 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "prefetches_queue_decision_context or capacity_queue_decision_warns_when_attempt_is_missing"`
  passed: `2 passed, 221 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k capacity`
  passed: `36 passed, 187 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_task_attempts.py -q`
  passed: `9 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/db/repositories.py tests/unit/control/test_worker.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

None.
