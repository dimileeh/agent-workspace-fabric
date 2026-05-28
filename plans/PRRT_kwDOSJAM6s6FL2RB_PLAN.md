# PRRT_kwDOSJAM6s6FL2RB Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6FL2RB` reports that companion/profile
service dependency validation accepts targets that cannot satisfy the compose
template's `condition: service_healthy` dependency requirement. In particular,
`agent` has no healthcheck, and profile or companion services without
`healthcheck_cmd` also cannot become healthy for dependency gating.

Scope is limited to dependency graph validation for companion/profile services
and focused regression tests.

## Requirements Checklist

- Reject dependencies on `agent`, because the rendered agent service does not
  define a healthcheck.
- Reject dependencies on any profile or companion service without
  `healthcheck_cmd`.
- Preserve existing unknown-target, name-collision, docker-mode, and cycle
  validation behavior.
- Add focused regression coverage for unhealthy dependency targets.
- Avoid broad AWF/GitHub-owned validation; record only focused local checks.

## Implementation Steps

1. Update `validate_companion_service_graph` to compute dependency targets that
   are known but not healthcheck-capable.
2. Raise a `ProfileResolutionError` with a dedicated reason code when
   dependency declarations target those services.
3. Update `tests/unit/node/test_companion_services.py` to expect rejection of
   `agent` dependencies and service-without-healthcheck dependencies, while
   preserving acceptance of healthcheck-capable targets.
4. Run targeted unit tests for companion service validation.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase.
