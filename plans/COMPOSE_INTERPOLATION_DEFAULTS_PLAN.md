# Compose Interpolation Defaults Plan

## Problem Statement And Scope

Address PR review comment `issue:4482045018` for Compose interpolation key collection. The parser currently collects plain `$VAR` references inside a braced interpolation default such as `${FOO:-$BAR}` even though Compose treats that default text literally for this use case.

Scope is limited to `awf.service.environment` interpolation-key parsing and focused unit coverage.

## Requirements Checklist

- Add a regression proving `$BAR` inside `${FOO:-$BAR}` is not collected as a Compose interpolation input.
- Preserve existing behavior for braced variables, plain variables, escaped dollar values, malformed braced expressions, and YAML-value-only collection.
- Do not change cache behavior; the review note about empty tuple caching is already correct and needs no code change.
- Run the narrow unit test surface that covers service log interpolation parsing.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_logs.py`.
2. Update string interpolation-key collection to skip the full braced expression body after collecting the outer variable.
3. Run the focused failing/passing tests.
4. Create validation evidence in `plans/COMPOSE_INTERPOLATION_DEFAULTS_VALIDATION.md`.
