# Review 4495131102 Capacity Deferred Stable Signature Plan

## Problem Statement And Scope

Review comment `issue:4495131102` identifies write amplification in local
capacity deferred-decision deduplication. The current blocker signature includes
mutable node allocation snapshots (`allocated` and derived `after`), so each
successful admission can make every still-blocked queued workspace look like a
new deferred decision even when the blocking reason, limit, and requested demand
are unchanged.

Scope is limited to deferred local-capacity queue-decision deduplication in
`src/awf/control/worker.py` and focused worker tests. Stored decision payloads
should still include the allocation snapshot for records that are written.

## Requirements Checklist

- [ ] Keep recording the first deferred local-capacity queue decision.
- [ ] Do not append another deferred decision when only `allocated` / `after`
      changes for the same blocker identity.
- [ ] Keep `allocated` and `after` in the stored blocker payload for audit
      context on written decisions.
- [ ] Continue recording a new deferred decision when stable blocker identity
      changes, such as dimension, reason code, limit, requested demand, or
      unsatisfiable classification.
- [ ] Keep ordered/defaulted capacity decisions and non-capacity queue decisions
      unchanged.
- [ ] Validate with focused worker tests and static checks for touched files.

## Implementation Steps

1. Update focused tests first so allocated-only capacity drift is expected to
   dedupe, and add direct helper coverage that the stable signature ignores
   `allocated` / `after` while preserving other blocker fields.
2. Run the focused tests and confirm the allocated-only regression fails before
   implementation.
3. Remove `allocated` and `after` from `_CAPACITY_BLOCKER_SIGNATURE_FIELDS`
   while leaving `_capacity_blocker_payload` unchanged.
4. Run the focused worker tests, lint, and mypy for the touched worker/test
   files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_decision_signature_helpers_reject_mismatches or requested_capacity_gate_dedupes_allocated_only_capacity_deferral_changes"`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate"`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`

Pass criteria: the allocated-only regression fails before the implementation
change, passes after it, and static checks exit successfully.
