# Review Level 4445667428 Validation

Plan reference: `plans/REVIEW_LEVEL_4445667428_PLAN.md`

## Requirement Status

- Remove the redundant `has_validation_evidence` condition without changing
  failure-causality behavior: Complete.
  - `src/awf/service/failure_causality.py` now returns early solely on missing
    workspace evidence, matching the equivalent prior behavior.
- Preserve the locked transition semantics in `worker.py` unless inspection
  shows a correctness-safe narrower lock window: Complete.
  - `WorkspaceRepository.get_for_update()` uses PostgreSQL `SELECT FOR UPDATE`
    for the target deployment, and the causality snapshot feeds the same
    transaction that writes the terminal transition.
- Document the lock-ordering rationale if the existing PostgreSQL row lock is
  the correct concurrency boundary: Complete.
  - `src/awf/control/worker.py` now documents that the stale-active and
    runtime-stranding paths intentionally keep causality loading and transition
    in one locked failure epoch.
- Run focused tests and lint for the touched Python modules: Complete.
- Commit the local fix with a conventional commit message referencing the
  review comment id: Complete.
  - This validation file is included in the local fix commit.
- Emit the required `AWF-VERDICT` line when complete: Complete.
  - The verdict is emitted after the local commit as required by the AWF
    comment-handling contract.

## Evidence

Files changed:

- `src/awf/service/failure_causality.py`
- `src/awf/control/worker.py`
- `plans/REVIEW_LEVEL_4445667428_PLAN.md`
- `plans/REVIEW_LEVEL_4445667428_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passed: 24 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py::test_fail_stale_active_execution_skips_status_mismatch tests/unit/control/test_worker_coverage_edges.py::test_fail_stale_active_execution_restores_primary_failure_row_fields -q`
  passed: 2 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py src/awf/control/worker.py`
  passed.

## Test Strategy Note

The guard simplification is behavior-preserving dead-code removal: whenever
`has_validation_evidence` was true, `workspace.failure_reason` was already the
non-empty `validation_failure` value, so `has_workspace_evidence` was also
true. Existing failure-causality tests cover the observable behavior.

## Gaps

No implementation gaps remain.
