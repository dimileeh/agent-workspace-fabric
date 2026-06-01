# PR349 Python Full Coverage Edge Plan

## Problem Statement and Scope

PR #349 fails CI only in `python-full-coverage`; `ci-required` fails because it
aggregates that required check. The CI-produced coverage XML reports combined
coverage at 98.9967%, which rounds to 99.00% but is still three coverage units
below the exact `fail_under = 99` gate.

Scope is limited to adding focused regression coverage for already-implemented
pre-push validation edge paths. Do not edit workflow, coverage, quality-gate, or
other protected configuration files.

## Requirements Checklist

- [ ] Preserve the configured coverage gate; do not skip, disable, or weaken it.
- [ ] Add targeted unit coverage for PR-owned uncovered pre-push validation edge
      paths.
- [ ] Keep production behavior unchanged unless a real bug is found.
- [ ] Run focused local tests only; leave broad AWF/GitHub validation to AWF
      after agent completion.
- [ ] Commit the fix locally on the current AWF branch.

## Implementation Steps

1. Use CI logs/artifacts to identify exact uncovered lines without rerunning the
   broad coverage suite locally.
2. Add small unit tests around existing helpers/error branches in
   `awf.runtime.pr_monitor_runner.pre_push_validation`.
3. Run the narrow test file(s) that cover the changed tests.
4. Record validation evidence in
   `plans/PR349_PYTHON_FULL_COVERAGE_EDGE_VALIDATION.md`.
5. Commit the plan, validation document, and test changes locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest <focused test file> -q`
  - Passes locally.
- CI artifact inspection remains the evidence for the exact global gap:
  downloaded `coverage.xml` from run `26781298228` shows three units needed for
  `>=99%`.

Full AWF/GitHub validation, including whole-repository coverage, is managed by
AWF after agent completion per the workspace contract.
