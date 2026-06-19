# PR608 Coverage Exact Threshold Plan

## Problem Statement and Scope

PR #608 fails the `python-full-coverage` CI job at the exact combined line+branch
threshold: GitHub Actions reports 78,445 of 79,238 opportunities covered, or
98.999%, below the required 99.00%. The coverage shards and all other required
jobs passed; `ci-required` fails only because `python-full-coverage` failed.

Scope is limited to adding a meaningful focused regression test for an uncovered
behavior in PR-touched code. Do not weaken coverage checks, edit workflow gates,
or run full local coverage.

## Requirements Checklist

- [ ] Diagnose the CI failure from the GitHub Actions log and identify a real
      uncovered behavior in changed code.
- [ ] Add or adjust a focused test that asserts behavior, not merely line
      execution.
- [ ] Run only targeted local validation for the changed test area.
- [ ] Record validation evidence in a matching validation document.
- [ ] Commit the fix locally with a conventional `fix(ci): ...` message.

## Implementation Steps

1. Use the GitHub Actions log for run `27825438839` to confirm the failing
   coverage totals and missing changed-code opportunities.
2. Add a regression test in the existing controls lifecycle guide test surface
   for the monitor-origin blocked guide path that reopens a merge candidate.
3. Verify the targeted test passes.
4. Create `plans/PR608_COVERAGE_EXACT_THRESHOLD_VALIDATION.md` with requirement
   status and focused command evidence.
5. Commit the scoped changes.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_guide_monitor_candidate.py -q`

Pass criteria: the targeted test file passes locally. Full AWF/GitHub coverage
validation is intentionally left to AWF after agent completion.
