# PRRT_kwDOSJAM6s6KznT8 Plan

## Problem Statement and Scope

The review thread reports that `_deposit_satisfied_conformance_report` rejects
an oversized synthesized conformance report and returns before the best-effort
`plan.md` copy runs. Scope is limited to preserving the safe plan deposit while
continuing to reject and clean up oversized conformance artifacts.

## Requirements Checklist

- Keep oversized synthesized conformance reports out of the served artifact
  directory.
- Remove stale `conformance.json` and temp conformance files when the
  synthesized report exceeds `MAX_ARTIFACT_CONTENT_BYTES`.
- Still run the existing hardened best-effort `plan.md` deposit when the report
  is oversized.
- Cover the behavior with a focused regression test.
- Use only narrow validation; full AWF/GitHub validation remains managed after
  agent completion.

## Implementation Steps

1. Update the existing oversized-report regression test to expect a safe plan
   copy while conformance artifacts are removed.
2. Run the focused test to confirm it fails against current code.
3. Change `_deposit_satisfied_conformance_report` so the oversized-report path
   skips only the report write, not the plan deposit.
4. Re-run the focused test.
5. Record validation results in `plans/PRRT_kwDOSJAM6s6KznT8_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py -q -k test_deposit_satisfied_conformance_report_rejects_oversized_report`
  - Passes with the regression test confirming no served conformance artifact,
    no temp conformance artifact, and deposited `plan.md`.
