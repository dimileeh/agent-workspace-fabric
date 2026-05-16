# Review 4454403868 Router Dependencies Plan

## Problem Statement And Scope

The OpenAPI auth contract patcher in `src/awf/api/app.py` detects auth-guarded
operations by checking `APIRoute.dependencies`. That misses auth injected at the
`APIRouter(dependencies=[Depends(require_api_token)])` level after routers are
included, because FastAPI stores the resolved dependency graph on
`APIRoute.dependant`.

Scope is limited to aligning OpenAPI required Authorization header patching with
FastAPI's resolved dependency graph. No endpoint auth behavior should change.

## Requirements Checklist

- Add a regression test proving router-level `require_api_token` dependencies
  are marked as required Authorization headers in generated OpenAPI.
- Update `_auth_required_operations` to inspect the resolved dependency graph
  rather than only route-level dependency declarations.
- Preserve existing route-level auth header behavior and malformed-schema guard
  behavior.
- Run focused OpenAPI tests and the OpenAPI drift check.
- Commit the fix locally without pushing or changing branches.

## Implementation Steps

1. Add a failing test in `tests/unit/api/test_openapi_artifact.py` with a small
   `FastAPI` app and an `APIRouter` that declares `Depends(require_api_token)` at
   the router level.
2. Run the focused test to confirm it fails against the current detector.
3. Update `src/awf/api/app.py` so auth detection walks `route.dependant`
   dependencies and identifies `require_api_token` anywhere in that graph.
4. Run focused OpenAPI tests and regenerate `openapi.json` only if drift exists.
5. Write validation evidence in
   `plans/REVIEW_4454403868_ROUTER_DEPENDENCIES_VALIDATION.md`.
6. Stage only changed files and commit with a review-comment-specific message.

## Assumptions/Changes

- In the FastAPI version used by this repository, direct
  `APIRouter(dependencies=[Depends(require_api_token)])` entries are already
  copied into `APIRoute.dependencies`. The failing regression therefore uses a
  router-level guard that depends on `require_api_token`, which proves the same
  resolved-dependency-graph gap without changing production router behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q`
  passes.
- `python scripts/generate_openapi.py --check` passes, or `openapi.json` is
  regenerated and the check passes.
