# PRRT_kwDOSJAM6s6F3FyC Plan

## Problem Statement And Scope

The review thread reports that custom planning artifact templates such as
`docs/{workspace_id}.md` still emit a broad `docs/ws_*.md` artifact pattern even
when the caller passes a concrete `workspace_id`. That broad pattern can cause
ordinary repository files like `docs/ws_protocol.md` to be filtered out of
inter-workspace overlap, lock, merge-queue, and staleness checks.

Scope is limited to internal plan artifact owned-path classification and its
unit coverage.

## Requirements Checklist

- When a concrete `workspace_id` is available, custom planning artifact
  templates should filter only the concrete workspace artifact path.
- Broad `ws_*` artifact patterns should remain available when no concrete
  workspace id is available.
- Ordinary repository files that happen to match `ws_*.md` should remain
  inter-workspace owned paths for known workspaces.
- Existing default `docs/awf-plans` artifact classification should keep working.

## Implementation Steps

1. Add a regression test for `docs/{workspace_id}.md` showing that
   `docs/ws_protocol.md` remains in `interworkspace_owned_paths` when
   `workspace_id` is known.
2. Confirm the new test fails against the current implementation.
3. Narrow `_internal_plan_artifact_paths_from_template` so known workspace ids
   produce concrete artifact paths instead of broad filename globs.
4. Update existing custom-profile tests to reflect the known-id behavior while
   preserving unknown-id wildcard behavior.
5. Run focused unit tests and focused lint for the touched files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py`

Full AWF/GitHub validation is intentionally left to the AWF post-agent workflow.
