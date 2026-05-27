# Companion YAML-Safe Paths Plan

## Problem Statement And Scope

An unresolved PR review thread reports that public companion requests can accept
repo-relative path values containing characters that break the raw
double-quoted YAML scalars in `docker/compose/workspace.base.yml.j2`. The scope
is limited to companion request schema validation and focused regression tests.

## Requirements Checklist

- Reject public companion repo-relative path fields that could break raw
  double-quoted YAML rendering.
- Cover `build_context`, `dockerfile`, `env_file`, repo-relative volume
  sources, and volume targets rendered in the same raw compose scalar.
- Keep valid repo-relative and named-volume companion requests unchanged.
- Add focused regression tests before implementation.
- Run only targeted validation owned by this change; leave broad AWF/GitHub
  validation to AWF after agent completion.

## Implementation Steps

1. Add failing regression cases to the companion invalid public contract schema
   tests.
2. Add a small schema-boundary helper that rejects YAML-unsafe path characters.
3. Apply the helper to repo-relative companion path validation.
4. Run the focused schema test file and any narrow lint needed for touched
   files.
5. Record validation evidence in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  must pass.

Full repository validation, full coverage gates, and CI-equivalent suites are
intentionally not run during this agent phase; AWF owns those after completion.
