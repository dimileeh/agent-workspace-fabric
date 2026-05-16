# Workspace Replay After Rate Limit Plan

## Problem Statement And Scope

PR thread `PRRT_kwDOSJAM6s6Cb3EH` reports that `POST /v1/workspaces` and
`POST /v2/workspaces` can return `429 WORKSPACE_CREATE_RATE_LIMITED` for an
existing idempotency-key replay if the process replay-key cache has been lost
or evicted. The change is scoped to workspace create replay ordering around
rate limiting.

## Requirements Checklist

- Add regression coverage for v1 and v2 workspace create replays that survive
  replay-key cache loss while the caller is over the workspace create rate
  limit.
- Preserve the existing behavior that fresh over-limit workspace create
  requests are rejected with `429`.
- Preserve idempotency conflict semantics for existing keys with different
  payloads.
- Keep disk admission and provider preflight behind rate-limit admission for
  fresh v2 requests.
- Do not push or switch branches; commit the local fix on the current AWF
  branch.

## Implementation Steps

1. Add a focused failing API regression test that clears the route replay-key
   cache after the initial successful create, then retries the same
   idempotency-key request after the rate limit is exhausted.
2. Add a repository-level existence helper for workspace idempotency keys.
3. Update v1 and v2 route handlers so a rate-limited idempotency-key request
   checks whether the key is already persisted; if it is, return the existing
   replay/conflict response instead of `429`.
4. Reuse the existing replay response helpers so payload comparison, warnings,
   provider readiness, and cache remembering stay centralized.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/api/test_workspaces.py`
  passes.
