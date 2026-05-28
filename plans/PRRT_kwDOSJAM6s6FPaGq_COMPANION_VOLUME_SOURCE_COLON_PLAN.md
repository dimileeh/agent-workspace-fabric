# PRRT_kwDOSJAM6s6FPaGq Companion Volume Source Colon Plan

## Context

Review thread `PRRT_kwDOSJAM6s6FPaGq` reports that repo-relative companion
volume sources containing `:` can pass public schema validation. Those sources
are later resolved to host paths and rendered with Docker Compose short volume
syntax, where `:` separates source, target, and mode fields.

## Goals

- Reject `:` in repo-relative companion volume sources.
- Preserve existing valid repo-relative and named-volume source behavior.
- Add focused API schema regression coverage for the reported case.

## Steps

1. Add a failing focused schema test for a repo-relative source like
   `data:ro/files`.
2. Update companion volume-source validation to reject `:` only on
   repo-relative volume sources.
3. Run the focused regression test, then the existing companion schema edge
   test that owns this validation surface.

## Validation

Focused checks only:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_repo_relative_volume_sources_with_colons -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_repo_relative_volume_sources_with_colons -q`

Full AWF/GitHub validation is managed by AWF after agent completion.
