# PRRT_kwDOSJAM6s6HD4nZ GC Payload Plan

## Problem Statement And Scope

The PR review thread reports that compose teardown failures from the single-workspace
fallback path are kept in `WorkspaceGCResult.compose_teardowns` but are omitted from
`WorkspaceGCResult.to_dict()` when the fallback candidate is not present in
`plan.candidates`. Operators can then see a partial GC result with no serialized
teardown failure evidence.

Scope is limited to the GC result payload and focused regression coverage for the
reported fallback case.

## Requirements Checklist

- Verify fallback compose teardown failures for off-plan candidates are serialized.
- Preserve existing per-candidate `compose_teardown` payloads for real candidates.
- Keep the change minimal and avoid broad GC refactors.
- Run only focused tests for the touched behavior; AWF/GitHub own broad validation.

## Implementation Steps

1. Add a focused regression assertion covering `run_workspace_filesystem_gc()` with
   a missing workspace and a failed fallback compose teardown.
2. Confirm the assertion fails before implementation when practical.
3. Update `WorkspaceGCResult.to_dict()` to include serialized compose teardown
   results independent of candidate serialization.
4. Re-run the targeted test covering the regression.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_gc_reports_failed_missing_workspace_compose_teardown -q`
  - Passes after the implementation and proves the off-plan fallback teardown
    failure is visible in the serialized payload.

Full AWF/GitHub validation is intentionally not run during this agent phase.
