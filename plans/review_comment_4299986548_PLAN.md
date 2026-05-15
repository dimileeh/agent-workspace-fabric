# Review Comment 4299986548 Plan

## Problem Statement and Scope

PR review comment `4299986548` reports that `POST /v1/workspaces` and
`POST /v2/workspaces` acquire the workspace idempotency advisory lock and query
the workspace table before request-admission rate limiting. A burst that sends
a new `Idempotency-Key` per create request can therefore force database
lock/read work even after the caller is over quota.

Scope is limited to REST workspace create admission ordering and regression
coverage. Existing idempotent replay semantics for known keys must be preserved.

## Requirements Checklist

- Add a failing regression proving over-limit fresh workspace create keys do not
  acquire the idempotency lock or perform the idempotency lookup.
- Preserve same-key idempotent replay behavior when the key is known from a
  prior successful create.
- Preserve v1 and v2 shared workspace-create rate-limit behavior and v2 disk
  admission ordering.
- Keep the implementation local to workspace create routing unless tests reveal
  a shared helper is necessary.

## Implementation Steps

1. Add targeted tests in `tests/unit/api/test_workspaces.py` mirroring the
   existing callback admission-order coverage.
2. Introduce a small per-app/request workspace idempotency replay-key cache in
   `src/awf/api/routes/workspaces.py`.
3. Check the replay-key cache before request admission. Known same-payload keys
   may take the DB replay path; fresh keys must pass request admission before
   any DB lock/read.
4. Remember successful create/replay keys after durable DB confirmation.
5. Run the narrow failing/passing tests, then route-focused lint if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py`
  passes.
