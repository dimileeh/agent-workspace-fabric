# Review 4495131102 Capacity Deferred Stable Signature Validation

Plan reference:
`plans/REVIEW_4495131102_CAPACITY_DEFERRED_STABLE_SIGNATURE_PLAN.md`

## Requirement Status

- Complete: the first deferred local-capacity queue decision is still recorded.
- Complete: allocated-only blocker drift no longer appends repeated deferred
  decisions for the same workspace attempt.
- Complete: `allocated` and `after` remain in the stored blocker payload for
  deferred decisions that are written.
- Complete: stable blocker identity still includes dimension, reason code,
  limit, requested demand, and unsatisfiable classification.
- Complete: ordered/defaulted capacity decisions and non-capacity queue
  decisions are unchanged because the change is limited to the deferred
  capacity blocker signature.
- Complete: focused worker tests, broader capacity-gate tests, ruff, and mypy
  passed.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_4495131102_CAPACITY_DEFERRED_STABLE_SIGNATURE_PLAN.md`
- `plans/REVIEW_4495131102_CAPACITY_DEFERRED_STABLE_SIGNATURE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_decision_signature_helpers_reject_mismatches or requested_capacity_gate_dedupes_allocated_only_capacity_deferral_changes"`
  - Result before implementation: failed because allocation-only drift produced
    a signature mismatch and three `LOCAL_CAPACITY_DEFERRED` rows.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_decision_signature_helpers_reject_mismatches or requested_capacity_gate_dedupes_allocated_only_capacity_deferral_changes"`
  - Result after implementation: passed, `2 passed, 230 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate"`
  - Result: passed, `25 passed, 207 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  - Result: passed.

## Gaps

No planned gaps remain.
