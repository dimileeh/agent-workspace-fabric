# REVIEW 4495131102 Provider Decision Bound Validation

Plan reference: `REVIEW_4495131102_PROVIDER_DECISION_BOUND_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing the capacity gate records provider deferral for the front suppressed candidate needed to reach a claimable workspace.
- Complete: Added a regression assertion showing a lower-priority provider-suppressed candidate after the current claim slot receives no unnecessary deferred `QueueDecision`.
- Complete: Preserved capacity scheduling correctness by filtering the full fetched page for eligibility while bounding only provider-recovery decision writes.
- Complete: Kept non-capacity scheduler behavior compatible with existing provider-cooldown coverage.
- Complete: Prepared changes for a local conventional commit without branch switching or pushing.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_4495131102_PROVIDER_DECISION_BOUND_PLAN.md`
- `plans/REVIEW_4495131102_PROVIDER_DECISION_BOUND_VALIDATION.md`

Validation commands:

- Failing-before evidence: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "provider_suppression_decisions_to_claim_slots"` failed because the tail suppressed workspace had an unexpected provider-recovery deferred decision.
- Passing-after evidence: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "provider_suppression_decisions_to_claim_slots or dispatches_oldest_satisfiable_candidate or provider_cooldown_defer_does_not_consume_ready_execution_limit"` passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py tests/unit/control/test_worker_coverage_edges.py -q -k "provider_recovery_filter or provider_cooldown or provider_model_circuit_open or provider_suppression_decisions_to_claim_slots"` passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py` passed.

## Remaining Gaps

None.
