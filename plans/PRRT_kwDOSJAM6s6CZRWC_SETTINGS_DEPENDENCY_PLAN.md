# PRRT_kwDOSJAM6s6CZRWC Settings Dependency Plan

## Problem Statement and Scope

The workspace create routes currently accept `settings` as `object` and resolve it through a helper that falls back to a bare `get_settings()` call. That fallback can bypass FastAPI dependency overrides and can mask direct-call tests that pass non-`Settings` objects.

Scope is limited to the workspace create route settings dependency and the direct-call tests that bypass FastAPI dependency injection.

## Requirements Checklist

- Keep `create_workspace` and `create_workspace_v2` settings parameters typed as `Settings = Depends(get_settings)`.
- Remove the workspace route-level fallback through `resolve_settings_dependency`.
- Update direct route tests to pass real `Settings` objects where they bypass FastAPI.
- Add regression coverage that would fail if the route settings dependency is widened back to `object`.

## Implementation Steps

1. Add a focused regression test for the workspace create route settings dependency annotation.
2. Update `src/awf/api/routes/workspaces.py` to use the injected `Settings` directly.
3. Update direct-call tests that currently omit settings or pass a `SimpleNamespace`.
4. Run targeted unit tests for the touched workspace route behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_route_error_edges.py tests/unit/api/test_workspaces_direct.py tests/unit/api/test_workspaces.py -q`
- Pass criteria: targeted tests pass without route fallback to bare `get_settings()` in workspace create routes.
