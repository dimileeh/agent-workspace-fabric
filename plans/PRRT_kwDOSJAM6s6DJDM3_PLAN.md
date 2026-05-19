# PRRT_kwDOSJAM6s6DJDM3 Plan

## Problem Statement and Scope

`collect_support_bundle()` accepts `compose_env_file`, but when callers omit
`provider_environ` it currently passes the raw process environment as
`provider_environ` to the doctor and status collectors. That makes the nested
collectors treat provider environment as explicit and prevents provider
readiness from seeing tokens that exist only in the compose env file.

Scope is limited to support-bundle provider environment resolution and the
regression coverage for direct helper calls.

## Requirements Checklist

- Add a failing regression test showing that a direct support bundle call with
  `compose_env_file` and no explicit `provider_environ` passes compose env
  values to both status and doctor collectors.
- Preserve explicit `provider_environ` precedence.
- Keep environment merging aligned with `local_service_environ()` so host
  environment values override compose env values.
- Keep the support bundle redaction path aware of compose env secrets.
- Run focused tests for the touched support bundle behavior.

## Implementation Steps

1. Add a unit test in `tests/unit/service/test_support_bundle.py` that captures
   status and doctor collector kwargs for a direct `collect_support_bundle()`
   call using only `compose_env_file`.
2. Confirm the new test fails against the current implementation.
3. Update `src/awf/service/support_bundle.py` to resolve provider environment
   from `compose_env_file` when `provider_environ` is omitted, using
   `local_service_environ()`.
4. Preserve existing behavior when `provider_environ` is explicitly supplied.
5. Re-run the focused support bundle tests.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py::test_support_bundle_resolves_provider_environment_from_compose_env_file -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q
```

Pass criteria: the focused regression fails before implementation, then passes
after the implementation along with the full support bundle unit test module.
