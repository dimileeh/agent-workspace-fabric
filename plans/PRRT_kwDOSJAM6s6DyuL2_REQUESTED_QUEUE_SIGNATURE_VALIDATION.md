# PRRT_kwDOSJAM6s6DyuL2 Requested Queue Signature Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DyuL2_REQUESTED_QUEUE_SIGNATURE_PLAN.md`

## Requirement Status

- Complete: Add a regression test showing that a workspace outside the bounded
  ID sample but inside the scheduler frontier changes the requested queue
  signature.
  - Evidence: `tests/unit/control/test_worker.py`
    `test_requested_capacity_queue_signature_changes_when_scheduler_frontier_changes_beyond_id_sample`.
  - TDD evidence: the new regression failed before implementation with an
    unchanged signature.
- Complete: Preserve bounded signature scans.
  - Evidence: `_requested_capacity_queue_signature` still applies
    `_REQUESTED_CAPACITY_QUEUE_SIGNATURE_LIMIT`; existing SQL compile coverage
    passed.
- Complete: Keep the signature based on requested work schedulable for the
  worker node.
  - Evidence: `src/awf/control/worker.py` preserves requested-status and
    node/nullable-node filters.
- Complete: Preserve existing signature sensitivity to queue composition and
  scheduler policy/profile fields.
  - Evidence: existing requested queue signature tests passed.
- Complete: Do not change branch, push, or weaken existing safety tests.
  - Evidence: only local files were edited; no push or branch operation was
    performed.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_queue_signature_changes_when_scheduler_frontier_changes_beyond_id_sample"`
  - Initial result before implementation: failed as expected.
  - Final result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_queue_signature or requested_capacity_gate_resets_resume_cursor_when_requested_queue_changes"`
  - Final result: 10 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Final result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  - Final result: passed.

## Gaps

None.
