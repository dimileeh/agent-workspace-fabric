# PRRT_kwDOSJAM6s6KztW2 Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6KztW2` reports that `_deposit_satisfied_conformance_report`
returns after an in-memory conformance report write `OSError`, so the fallback
served artifact directory may miss `plan.md` even though the worktree plan is
available. Scope is limited to this fallback deposit path and its focused
regression coverage.

## Requirements Checklist

- Verify the current code path and existing tests before changing behavior.
- Preserve the non-fatal handling of conformance report artifact write failures.
- Ensure a failed in-memory `conformance.json` write still attempts the hardened
  best-effort `plan.md` deposit when the artifact directory exists.
- Keep stale `conformance.json` and `.conformance.json.tmp` cleanup behavior.
- Do not broaden validation beyond focused tests; AWF/GitHub own broad gates
  after agent completion.

## Implementation Steps

1. Update the focused regression test for report write `OSError` to expect
   `plan.md` deposition.
2. Run that focused test and confirm it fails against the current early return.
3. Remove only the early return that prevents the later plan deposit.
4. Re-run the focused test.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6KztW2_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py::test_post_validation_conformance_report_deposit_oserror_is_non_fatal -q`
  - Passes after implementation.
  - Fails before implementation because `plan.md` is absent.

Full AWF/GitHub validation is intentionally not run in this agent phase.
