# Capacity Queue Batch Prefetch Plan

## Problem Statement And Scope

PR review comment `issue:4495131102` reports that the local capacity scheduler records
queue decisions for blocked requested candidates with two sequential reads per workspace:
the task attempt and the latest queue decision. A fully blocked capacity page can contain
up to the scheduler candidate fetch limit, so those reads should be batched once per page.

Scope is limited to the requested-workspace local capacity gate and repository support
needed for batched attempt lookup. Existing queue-decision semantics, deduplication, and
missing-attempt warning behavior must remain unchanged.

## Requirements Checklist

- Add a regression test proving capacity-page processing does not call per-workspace
  `TaskAttemptRepository.get_by_workspace_id` or `QueueDecisionRepository.list_for_workspace`.
- Batch-fetch task attempts for all page candidates before the capacity loop.
- Batch-fetch latest queue decisions for all page candidates before the capacity loop.
- Preserve deferred-decision deduplication, previous resource-summary carry-forward, and
  missing-attempt warning behavior.
- Keep changes scoped to worker scheduling and repository helpers.

## Implementation Steps

1. Add a unit regression around `_claim_requested_capacity_candidates` / `run_once` that
   creates multiple blocked requested workspaces, patches single-row lookups to fail, and
   verifies capacity decisions are still recorded.
2. Add a `TaskAttemptRepository` batch lookup by workspace IDs.
3. Prefetch task attempts and latest queue decisions once per candidate page in
   `_claim_requested_capacity_candidates`.
4. Pass prefetched context into `_record_capacity_queue_decision`, while keeping its
   existing direct-call fallback for other tests and call sites.
5. Run the focused failing/passing test, then the relevant worker test slice and lint/type
   checks as practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k capacity`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_task_attempts.py -q` passes
  if repository tests are touched.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/db/repositories.py tests/unit/control/test_worker.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf` passes if runtime permits.
