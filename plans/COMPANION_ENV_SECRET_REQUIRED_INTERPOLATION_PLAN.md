# Companion Env Secret Required Interpolation Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6FUUoT` reports that required companion
environment secrets are rendered as plain Docker Compose interpolation
references. During PR-monitor resume, AWF reuses the persisted compose file via
`ensure_project_up()` and does not rerun companion secret resolution, so a
missing worker environment variable can be silently substituted as an empty
string by Compose.

Scope is limited to companion env-backed secret rendering and focused unit
tests/documentation for this review thread.

## Requirements Checklist

- Required companion env secrets must render a Compose required interpolation
  form so persisted compose files fail if the source variable is unset.
- Required companion env secrets must not place raw secret values into AWF
  service objects or rendered compose YAML.
- Existing behavior allowing an explicitly empty source value must remain
  intact.
- Optional missing companion env secrets must continue to be omitted.
- Validation must use focused tests only; full AWF/GitHub validation remains
  managed after agent completion.

## Implementation Steps

1. Update the companion env secret resolver to render required source refs with
   Compose's unset-only required interpolation.
2. Add/update focused unit assertions for companion conversion, stack launcher
   propagation, and rendered compose YAML.
3. Run focused unit tests for the touched node behavior.
4. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py -q`
  must pass.
- Do not run broad coverage, full repository suites, or CI-equivalent commands
  in this agent phase.
