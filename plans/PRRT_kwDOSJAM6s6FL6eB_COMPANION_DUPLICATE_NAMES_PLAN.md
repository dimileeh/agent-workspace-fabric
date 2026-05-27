# PRRT_kwDOSJAM6s6FL6eB Companion Duplicate Names Plan

## Problem Statement

Inline review thread `PRRT_kwDOSJAM6s6FL6eB` reports that
`validate_companion_service_graph` silently collapses duplicate companion
service names when it builds a set of names, and `_companion_service_dependency_cycle`
silently overwrites earlier companion graph nodes when it builds the dependency
graph. This can let a malformed workspace companion configuration pass with a
corrupted dependency graph.

## Scope

- Update companion-service graph validation only.
- Add a focused regression test for duplicate materialized companion names.
- Keep existing profile-service collision, unknown dependency, unhealthy
  dependency, and cycle behavior intact.
- Do not run broad AWF/GitHub validation; AWF owns broad validation after the
  agent phase.

## Requirements Checklist

- Detect duplicate `companion.spec.name` values before dependency validation and
  cycle graph construction.
- Raise `ProfileResolutionError` with reason code
  `COMPANION_SERVICE_NAME_COLLISION` for duplicate companion names.
- Include the duplicate companion service name(s) in the error message.
- Preserve existing profile service collision behavior and reason code.
- Validate with a targeted unit test command for `tests/unit/node/test_companion_services.py`.

## Implementation Steps

1. Add a failing unit test that builds two `MaterializedCompanionService`
   objects with the same `WorkspaceCompanionSpec.name` and asserts the collision
   error contains the duplicate name.
2. Run the targeted test and confirm the new regression fails against current
   code.
3. Update `validate_companion_service_graph` to compute duplicate companion
   names before deriving the companion-name set and fail fast.
4. Run the targeted companion-service unit tests.
5. Create the validation document with requirement status and focused evidence.
