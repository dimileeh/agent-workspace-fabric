# Companion Named Volume Source Validation Plan

## Problem Statement and Scope

An unresolved PR review thread reports that public companion volume sources that
are not repo-relative are accepted as arbitrary named-volume strings. Unsafe
values such as `api-cache:ro` can alter Docker Compose short-syntax structure,
and YAML-breaking characters can produce invalid compose files after worktrees
are created.

Scope is limited to companion request validation and focused regression tests for
the public API schema. Compose rendering and profile-owned service volume
behavior are out of scope unless the regression shows the schema fix cannot
block unsafe public input.

## Requirements Checklist

- Reject companion named volume sources that contain `:`, whitespace, newlines,
  or other characters outside a conservative Docker-safe volume-name pattern.
- Preserve accepted companion volume sources for repo-relative mounts such as
  `./fixtures` and safe named volumes such as `api-cache`.
- Keep existing volume target validation unchanged.
- Run only focused validation commands for the changed behavior; broad AWF/GitHub
  validation remains owned by AWF after this agent phase.

## Implementation Steps

1. Add a failing schema regression for unsafe named-volume sources.
2. Add a conservative named-volume validator to the companion schema path.
3. Re-run the focused schema tests and any narrow lint/type checks needed for
   the touched file.
4. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  must pass.
