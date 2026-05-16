# PRRT_kwDOSJAM6s6Cfh_B Plan

## Problem Statement And Scope

The workspace create endpoints currently let cold idempotent replays consume
fresh workspace-create quota when the replay key has fallen out of the
process-local cache and the caller still has quota remaining. This affects both
`POST /v1/workspaces` and `POST /v2/workspaces`.

Scope is limited to workspace create idempotency replay ordering and regression
coverage for the reported quota accounting bug.

## Requirements Checklist

- Durable idempotency replays for persisted workspace keys must return before
  workspace-create quota admission.
- Fresh idempotency keys must still be admitted through the workspace-create
  rate limiter before creating a workspace.
- The durable replay lookup must remain lock-scoped and must not rely on a
  pre-lock existence probe.
- The behavior must apply to both v1 and v2 workspace create paths.
- Add regression coverage proving a cold replay with quota remaining does not
  spend the next fresh-create slot.

## Implementation Steps

1. Add a focused failing regression test for v1 and v2 create endpoints with a
   quota limit of two: fresh create, cache-cleared replay, then a distinct fresh
   create that must still be admitted.
2. Move the locked durable replay check ahead of `admit_request_async` for
   idempotency-key requests in both create handlers.
3. Reuse the same replay-cache warming behavior for durable replay hits and
   conflicts.
4. Run the narrow workspace API test subset, then run targeted lint/type checks
   if the change surface warrants it.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py`
  must pass.
