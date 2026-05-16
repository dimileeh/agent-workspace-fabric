# Review Thread PRRT_kwDOSJAM6s6CfVG8 Plan

## Problem Statement and Scope

The callback registration route can reject an over-quota duplicate
`Idempotency-Key` request with 429 while the original registration is still
committing, because callback durable replay probes do not wait on a
transaction-scoped idempotency lock before reading the callback subscription row.

Scope is limited to callback registration idempotency serialization and the
review-thread regression coverage.

## Requirements Checklist

- Add regression coverage proving over-quota callback duplicate replay uses a
  serialization point before durable replay lookup.
- Ensure callback registrations and replays use the same PostgreSQL advisory
  lock namespace for a given callback `Idempotency-Key`.
- Preserve existing rate-limit behavior for fresh over-quota callback keys.
- Keep the change scoped to callback idempotency behavior.

## Implementation Steps

1. Add a focused unit test in `tests/unit/api/test_callbacks.py` that fails when
   callback replay lookups happen without first acquiring the idempotency lock.
2. Add callback subscription idempotency advisory locking to the repository and
   call it from callback registration and durable replay paths.
3. Run the focused callback tests, then targeted lint/type checks if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py src/awf/service/callbacks.py tests/unit/api/test_callbacks.py`
  must pass.
