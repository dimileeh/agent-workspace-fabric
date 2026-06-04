# PRRT_kwDOSJAM6s6HEB0G Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6HEB0G` reports that `run_workspace_filesystem_gc()` can run a successful fallback compose teardown for an empty GC plan, but later derives secret-lease and reservation side-effect workspace IDs only from `plan.candidates`. That leaves side effects active for the workspace whose runtime was torn down.

Scope is limited to single-workspace filesystem GC fallback compose teardown behavior and its focused regression coverage.

## Requirements Checklist

- Verify successful fallback compose teardown contributes its workspace ID to terminal side-effect cleanup.
- Preserve failed fallback teardown behavior so leases and reservations are not released when compose teardown fails.
- Keep the change minimal and avoid unrelated GC refactors.
- Add focused regression coverage for the reported path.

## Implementation Steps

1. Add a regression test for a preserved terminal workspace with no GC candidate, a successful fallback compose teardown, one active secret lease, and one active resource reservation.
2. Confirm the regression fails before implementation when practical.
3. Update GC side-effect workspace ID derivation to include successful fallback teardown workspace IDs while still excluding failed teardowns.
4. Run the focused regression test, then the narrow affected GC test file if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_fallback_compose_teardown_releases_runtime_side_effects -q`
  - Passes only after fallback side-effect cleanup includes the fallback workspace ID.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py -q`
  - Passes without regressions in the nearby fallback GC behavior.

Full AWF/GitHub validation is managed by AWF after agent completion and is intentionally not run here.
