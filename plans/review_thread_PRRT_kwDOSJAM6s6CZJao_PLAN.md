# Review Thread PRRT_kwDOSJAM6s6CZJao Plan

## Problem Statement and Scope

The workspace route module has a local helper that resolves FastAPI's
`Depends(get_settings)` default when route handlers are called directly in unit
tests. The same direct-call support is needed by callback routes, so the helper
should live in shared API dependency code instead of `workspaces.py`.

## Requirements Checklist

- Add regression coverage for direct callback route calls that omit explicit
  `settings`.
- Move the settings-resolution helper into `src/awf/api/deps.py`.
- Reuse the shared helper from both workspace and callback route modules.
- Preserve existing ASGI behavior and error response contracts.
- Validate with targeted unit tests for dependencies, callbacks, and workspace
  direct calls.

## Implementation Steps

1. Add a failing direct-call callback test that exercises omitted `settings`.
2. Add a shared settings dependency resolver in `awf.api.deps`.
3. Replace the local workspace helper with the shared resolver.
4. Resolve callback route `settings` through the shared helper.
5. Run targeted tests and record validation evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_direct_call_uses_default_settings_dependency -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py tests/unit/api/test_workspaces_direct.py -q`

Pass criteria: all targeted tests pass, and the callback direct-call regression
would fail before the shared helper is wired into `callbacks.py`.
