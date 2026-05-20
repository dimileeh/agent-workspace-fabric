# Capacity Resume Cursor Queue Signature Validation

Plan reference: `capacity_resume_cursor_queue_signature_PLAN.md`

## Requirement Status

- Add a regression test proving a new fitting high-priority requested workspace
  inserted ahead of a stored capacity resume cursor is claimed on the next poll:
  Complete.
- Keep the bounded blocked-page resume behavior when the requested queue is
  unchanged: Complete.
- Reset the capacity resume cursor when the requested queue for the worker's
  local node scope changes: Complete.
- Preserve existing allocated-capacity signature invalidation: Complete.
- Avoid weakening existing scheduler priority, capacity, and bounded-scan tests:
  Complete.

## Evidence

Changed files:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/capacity_resume_cursor_queue_signature_PLAN.md`
- `plans/capacity_resume_cursor_queue_signature_VALIDATION.md`

Test-first evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "resets_resume_cursor_when_requested_queue_changes"` failed before the worker change with `assert 0 == 1`.

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "capacity_gate_resets_resume_cursor_when_requested_queue_changes or requested_capacity_gate_resumes_after_bounded_blocked_scan"` passed: 2 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py` passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q` passed: 209 passed.

## Remaining Gaps

None.
