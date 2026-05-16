# PRRT_kwDOSJAM6s6CghuR Plan

## Problem Statement And Scope

An unresolved review thread reports that `POST /v2/workspaces` can return
`IDEMPOTENCY_CONFLICT` only while the in-process replay-key cache is warm. The
specific replay is a valid v2 auto-profile retry: the original request used
`workspace.profile_ref: null`, and the retry omitted `workspace`, which parses
as `profile_ref: "auto"`. The durable v2 matcher accepts this pair, but the
cache compares raw request hashes first.

Scope is limited to workspace-create idempotency routing and focused regression
coverage for the reported warm-cache behavior.

## Requirements Checklist

- Add a failing regression test for the reported v2 replay shape.
- Ensure warm-cache v2 replays consult durable v2 matching before returning a
  cached raw-hash conflict.
- Preserve existing v2 conflict behavior for genuinely different payloads.
- Preserve no-create behavior when a known replay key has no durable row.
- Do not change branch, push, or weaken existing regression assertions.

## Implementation Steps

1. Add a unit test that creates a v2 workspace with `workspace.profile_ref:
   null`, then replays with `workspace` omitted under the same idempotency key
   on the same app instance.
2. Confirm the new test fails with the current warm-cache behavior.
3. Update `create_workspace_v2` so a cached v2 hash mismatch falls through to
   durable replay matching before deciding conflict.
4. Run the targeted idempotency/API tests, plus lint on touched Python files if
   practical.
5. Record validation results in `plans/PRRT_kwDOSJAM6s6CghuR_VALIDATION.md`.
