# Review Level 4445667428 Plan

## Problem Statement And Scope

Address the current review-level comment `issue:4445667428` for failure
causality preservation. The evidence raises two points:

- `has_validation_evidence` in `load_failure_causality_snapshot` is dead guard
  logic because validation evidence already implies workspace evidence.
- The stale-active and runtime-stranding worker paths now hold a PostgreSQL row
  lock while loading causality evidence; confirm and document that this is
  intentional rather than accidentally expanding the critical section.

No branch changes, pushes, rebases, or GitHub comments are in scope.

## Requirements Checklist

- Remove the redundant `has_validation_evidence` condition without changing
  failure-causality behavior.
- Preserve the locked transition semantics in `worker.py` unless inspection
  shows a correctness-safe narrower lock window.
- Document the lock-ordering rationale if the existing PostgreSQL row lock is
  the correct concurrency boundary.
- Run focused tests and lint for the touched Python modules.
- Commit the local fix with a conventional commit message referencing the
  review comment id.
- Emit the required `AWF-VERDICT` line when complete.

## Implementation Steps

1. Inspect the causality guard and worker transition paths.
2. Simplify `_primary_failure_snapshot` by removing the dead validation
   evidence branch.
3. Add a short worker comment explaining why causality is loaded under the row
   lock before the failure transition.
4. Run focused failure-causality and worker tests plus ruff on touched files.
5. Write `plans/REVIEW_LEVEL_4445667428_VALIDATION.md` with evidence and any
   remaining gaps.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py::test_fail_stale_active_execution_skips_status_mismatch tests/unit/control/test_worker_coverage_edges.py::test_fail_stale_active_execution_restores_primary_failure_row_fields -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py src/awf/control/worker.py`
  must pass.
