# Review Issue 4460873446 Plan

## Problem Statement And Scope

Address the review-level Greptile comment for PR #256. The comment raises three quality concerns in the request admission hardening slice:

- direct-call paths without `Request.app.state` can share process-global limiter/cache state;
- callback replay cache conflicts currently promote an entry before the payload hash guard;
- v1 and v2 workspace creation share the same `workspace_create` admission bucket without an explicit regression test or operator-facing documentation.

Scope is limited to request admission helpers, callback replay-cache behavior, workspace admission tests, configuration documentation, and this plan/validation pair.

## Requirements Checklist

- [ ] Remove or avoid process-global fallback state accumulation for direct-call request admission and callback replay-cache paths that lack app state.
- [ ] Preserve normal FastAPI app-state scoped limiter/cache behavior.
- [ ] Move callback replay-cache LRU promotion so conflicting payloads do not refresh eviction priority.
- [ ] Add regression coverage for conflict non-promotion.
- [ ] Add regression coverage proving `request=None` admission calls do not accumulate shared quota across tests/direct calls.
- [ ] Confirm the shared v1/v2 workspace-create bucket as intentional with focused coverage and configuration documentation.
- [ ] Run the narrow tests and lint/type checks needed for the touched area.

## Implementation Steps

1. Add failing tests for:
   - `admit_request(None, ...)` not sharing a limiter between calls;
   - callback replay-cache conflicts not promoting an entry;
   - v1 and v2 workspace creates sharing the configured workspace-create quota.
2. Update `request_admission_limiter()` to use app state when available, request-object-local state when possible, and a fresh fallback when `request is None`.
3. Update callback replay-cache lookup similarly for direct calls without app state, and move `move_to_end()` after the request hash comparison.
4. Clarify the workspace-create rate-limit setting description to say it covers both v1 and v2 create routes together.
5. Run targeted tests first, then the configured ruff/mypy commands for the touched Python surface.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py tests/unit/api/test_workspaces.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: all listed commands complete successfully, and the validation doc records any remaining gaps.
