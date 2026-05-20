# Requested Capacity Profile Signature Plan

## Problem Statement and Scope

An unresolved PR review thread reports that the requested-capacity resume cursor
can be reused after a queued workspace profile changes because the queue
signature digest excludes `Workspace.resolved_profile`. Default capacity demand
uses the resolved profile, including DinD slot demand, when no active
reservation exists. The fix is scoped to requested-capacity queue signature
calculation and its regression tests.

## Requirements Checklist

- Add a regression test proving the requested-capacity queue signature changes
  when only a queued workspace's `resolved_profile` changes while count,
  timestamp maxima, max id, task class, agent, and task policy remain stable.
- Include `resolved_profile` in both PostgreSQL and non-PostgreSQL queue
  signature digest paths.
- Preserve the existing resume cursor behavior for unchanged queue/allocation
  signatures.
- Keep edits focused to the worker scheduler signature and directly related
  tests.

## Implementation Steps

1. Add the failing regression test in `tests/unit/control/test_worker.py`.
2. Update `_requested_capacity_queue_signature` to read and digest
   `Workspace.resolved_profile` in the SQLite/fallback path.
3. Update the PostgreSQL aggregate digest to include
   `Workspace.resolved_profile`.
4. Update `_requested_capacity_queue_digest_payload` and existing tests to
   account for the new digest field.

## Verification Commands and Pass Criteria

- Run the new regression before implementation and confirm it fails.
- Run the affected worker unit tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`.
- Run targeted lint for changed Python files:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`.

Pass criteria: the regression fails before the code change, then the affected
unit tests and lint pass after implementation.
