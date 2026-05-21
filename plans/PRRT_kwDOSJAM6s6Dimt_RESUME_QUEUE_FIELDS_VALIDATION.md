# PRRT_kwDOSJAM6s6Dimt Resume Queue Fields Validation

Plan reference: `PRRT_kwDOSJAM6s6Dimt_RESUME_QUEUE_FIELDS_PLAN.md`

## Requirement Status

- Complete: Added a regression test that keeps requested row count, max
  timestamps, max id, and requested membership stable while changing a queued
  workspace scheduler policy.
- Complete: Updated the requested-capacity queue digest to include per-row queue
  fields used for ordering and provider filtering: id, created_at, task_class,
  agent, and task_policy.
- Complete: Kept PostgreSQL and non-PostgreSQL signature paths aligned around
  the same queue field set.
- Complete: Preserved the existing `_RequestedCapacityQueueSignature` tuple
  shape.
- Complete: Ran focused worker tests and static checks.

## Evidence

Changed files:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "scheduler_policy_changes"` failed before the implementation change with an unchanged signature.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_queue_signature"` passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate_resets_resume_cursor_when_requested_queue_changes or requested_capacity_queue_signature"` passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

No remaining gaps.
