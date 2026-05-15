# Request Admission Limiter Lock Plan

## Problem Statement and Scope

The PR review thread reports that `RequestAdmissionLimiter` mutates process-local
shared quota state without synchronization. Because the limiter instance is
shared through app state, concurrent requests can read the same bucket count and
both pass a fixed-window limit.

Scope is limited to the request-admission limiter and focused regression tests.
No API behavior, metadata shape, identity derivation, or route policy should
change.

## Requirements Checklist

- Add a regression test showing concurrent admissions for the same identity do
  not exceed the configured limit.
- Guard all reads and mutations of limiter bucket/prune state with a lock.
- Preserve existing fixed-window behavior, retry metadata, and pruning behavior.
- Keep the change local to request admission and its tests.
- Validate with the narrow unit tests and static checks appropriate to the
  touched files.
- Commit the fix locally without pushing.

## Implementation Steps

1. Inspect existing request-admission tests and add a focused concurrency
   regression that fails without synchronization.
2. Add a lock to `RequestAdmissionLimiter` and use it around admission state
   reads, mutations, and pruning.
3. Run the new regression first, then the request-admission unit tests and
   relevant static checks.
4. Create a validation document mapping results back to this plan.
5. Stage only changed files and commit with the review-thread id in the message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py tests/unit/api/test_deps.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes, unless an unrelated pre-existing failure is documented.
