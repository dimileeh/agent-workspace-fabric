# PRRT_kwDOSJAM6s6FPNFr Plan

## Problem Statement And Scope

The review thread reports that `companion_service_from_materialized` resolves a companion
`dockerfile` against the companion checkout root even when `build_context` points at a
subdirectory. Docker Compose interprets `build.dockerfile` relative to `build.context`, so the
default `Dockerfile` must stay relative to the rendered build context.

Scope is limited to companion service materialization and focused regression coverage for that
behavior.

## Requirements Checklist

- Preserve absolute host resolution for companion `build_context`.
- Keep the default companion `Dockerfile` relative to the rendered build context.
- Preserve support for explicit repo-relative companion Dockerfile paths without allowing checkout
  escapes.
- Keep existing companion path safety behavior for `build_context`, `env_file`, and repo-relative
  volume sources.
- Do not run broad AWF/GitHub-owned validation; run only targeted unit tests for changed behavior.

## Implementation Steps

1. Add failing regression coverage in `tests/unit/node/test_companion_services.py` for a companion
   with `build_context="services/api"` and the default `dockerfile`.
2. Update companion Dockerfile path conversion in `src/awf/node/companion_services.py`.
3. Adjust any directly affected focused unit expectations.
4. Run targeted tests for companion services and stack-launcher companion rendering.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py -q -k companion`

Pass criteria: both focused commands pass. Full AWF/GitHub validation remains managed by AWF after
agent completion.
