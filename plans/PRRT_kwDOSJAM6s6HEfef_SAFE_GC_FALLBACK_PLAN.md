# PRRT_kwDOSJAM6s6HEfef Safe GC Fallback Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6HEfef` reports that
`run_workspace_filesystem_gc()` fabricates a compose teardown fallback candidate
for every preserved single-workspace classification. That can tear down runtime
resources for rows explicitly preserved by `WORKSPACE_CLEANUP_DISABLED` or
`FAILED_WORKSPACE_TRIAGE_PRESERVED`.

Scope is limited to single-workspace filesystem GC fallback compose teardown
selection and focused unit coverage.

## Requirements Checklist

- Keep the existing missing-workspace fallback compose teardown behavior.
- Keep established completed-workspace preserved fallback behavior for monitor
  cleanup paths such as `COMPLETED_PR_NOT_MERGED`.
- Do not run compose teardown or runtime side-effect revocation for
  `WORKSPACE_CLEANUP_DISABLED`.
- Do not run compose teardown or runtime side-effect revocation for
  `FAILED_WORKSPACE_TRIAGE_PRESERVED`.
- Add focused regression tests for the unsafe preserved reasons.
- Run only targeted tests; broad AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Add failing focused tests around single-workspace GC fallback teardown for
   cleanup-disabled and triage-preserved workspaces.
2. Add a small helper/allowlist in `src/awf/service/gc.py` so preserved fallback
   candidates are only created for safe completed-workspace reasons.
3. Run the targeted GC tests covering the new negative cases and existing
   positive fallback cases.
4. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6HEfef_SAFE_GC_FALLBACK_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py -q -k "single_workspace_gc_tears_down_compose_for_preserved_workspace or single_workspace_fallback_compose_teardown_releases_runtime_side_effects or cleanup_disabled or triage_preserved"`
  - Passes after implementation.
