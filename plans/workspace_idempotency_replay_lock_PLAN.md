# Workspace Idempotency Replay Lock Plan

## Problem Statement And Scope

PR thread `PRRT_kwDOSJAM6s6CfJDj` reports that rate-limited workspace create
retries can return `429 WORKSPACE_CREATE_RATE_LIMITED` before waiting on the
per-key idempotency advisory lock. This affects both REST v1 and v2 create
routes when a duplicate `Idempotency-Key` arrives while the original create is
still in flight and the process-local replay cache does not yet know the key.

Scope is limited to workspace create idempotency replay ordering after request
admission rejection.

## Requirements Checklist

- Add a regression proving over-quota duplicate workspace creates replay after
  taking the idempotency lock even when a pre-lock existence probe would miss.
- Preserve rate limiting for genuinely fresh over-quota idempotency keys.
- Preserve the no full-table replay-key warmup behavior for rejected fresh keys.
- Apply the same ordering semantics to v1 and v2 create helpers.
- Keep changes scoped to the route helper and focused API tests.

## Implementation Steps

1. Update the focused workspace API tests to assert rejected duplicates do not
   trust a pre-lock existence probe.
2. Run the narrow failing test selection before implementation.
3. Change `_workspace_create_v1_durable_replay_after_rejection` and
   `_workspace_create_v2_durable_replay_after_rejection` to use the replay
   helper first, which acquires the advisory lock before reading the row.
4. Keep fresh over-quota keys returning `429` when no row exists after the lock.
5. Run targeted tests and relevant lint/type checks.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py`
  passes.
- Validation document records each requirement as complete or explains any gap.
