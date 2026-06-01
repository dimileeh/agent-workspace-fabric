# COMMENT_3336797582 Workspace Error Response Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6GO8Wc` reports duplicated 409 JSON error
responses in `src/awf/api/routes/workspaces.py` for workspace host-port create
and retry failures. The fix should remove the duplicated response construction
without changing the public API response status, error code, message, or detail
payloads.

## Requirements Checklist

- [ ] Add a focused route-error test that proves the shared 409 response helper
      preserves the structured error payload.
- [ ] Extract shared workspace structured-error response construction.
- [ ] Use a single tuple handler for create host-port create failures.
- [ ] Use a single tuple handler for retry host-port create failures and let
      `WorkspaceRetryError` continue handling retry-native failures.
- [ ] Do not run broad AWF/GitHub-owned validation; record only focused local
      checks.

## Implementation Steps

1. Add a targeted test in `tests/unit/api/test_route_error_edges.py` for the
   shared conflict response shape.
2. Confirm that test fails before implementation.
3. Add a private structured-error helper in `workspaces.py`.
4. Replace duplicated handlers with tuple catch clauses and shared helper calls.
5. Run focused route-error test and ruff on changed Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_route_error_edges.py::<test-name> -q`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_route_error_edges.py`
  - Reports no lint errors.

Full AWF/GitHub validation remains owned by AWF after this agent phase.
