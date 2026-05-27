# Companion Dependency Cycle Rejection Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6FKbQk` reports that
`validate_companion_service_graph` accepts circular companion/profile
dependencies because every dependency target is known. That lets AWF render a
Compose file with a circular `depends_on` graph and fail later during launch
after companion worktrees have already been materialized.

Scope is limited to rejecting circular service dependency graphs in the
existing companion/profile validation path and adding focused regression tests.

## Requirements Checklist

- Add regression coverage for a self-dependent companion.
- Add regression coverage for a cycle between requested companions.
- Preserve existing unknown dependency validation behavior and reason code.
- Raise `ProfileResolutionError` before Compose launch for circular dependency
  graphs, with a stable reason code.
- Keep validation local to companion/profile service graph validation.

## Implementation Steps

1. Add focused unit tests in `tests/unit/node/test_companion_services.py` for
   self and multi-service circular companion dependencies.
2. Confirm the new tests fail against the current implementation when
   practical.
3. Update `src/awf/node/companion_services.py` to detect cycles after unknown
   dependency validation succeeds.
4. Run targeted unit tests for the companion service validator only.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- Targeted failing check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
- Targeted passing check after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`

Pass criteria: the focused companion service unit tests pass. Full AWF/GitHub
validation remains managed by AWF after agent completion per workspace
contract.
