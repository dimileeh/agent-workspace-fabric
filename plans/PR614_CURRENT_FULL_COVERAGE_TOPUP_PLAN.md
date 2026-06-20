# PR614 Current Full Coverage Top-Up Plan

## Problem Statement and Scope

PR #614 CI run `27861705439` failed only the aggregate
`python-full-coverage` job. The combined coverage artifact reports
`79687/80546` covered line+branch opportunities, or `98.93%`, below the
required `99.00%`; the exact gate needs at least 54 additional covered
opportunities.

The downloaded `full-coverage-report` artifact shows the largest PR-touched
remaining gap is `src/awf/node/git_manager.py` with 30 missed opportunities in
mirror hooks-path repair, linked-worktree path parsing, and agent-writable Git
target selection behavior. Scope is limited to behavior-focused tests for
reachable code. Do not edit workflow/quality-gate configuration, lower
thresholds, skip checks, or run broad AWF/GitHub-owned validation locally.

## Requirements Checklist

- Inspect the current failed CI run and combined `coverage.xml` missing
  opportunities before changing tests.
- Add meaningful focused tests for reachable uncovered behavior in PR-touched
  code, starting with `node/git_manager.py`.
- If the first focused top-up cannot plausibly cover the threshold shortfall,
  add one adjacent focused behavior top-up from the current artifact rather than
  broad or shallow tests.
- Keep changes minimal and avoid production refactors unless a bug is found.
- Run only focused pytest/coverage commands for changed test files or targeted
  modules.
- Record validation evidence and note that full AWF/GitHub validation remains
  owned by AWF after agent completion.

## Implementation Steps

1. Add failing behavior tests in existing node Git manager test modules for
   uncovered hooks-path repair and linked-worktree edge cases.
2. Run the narrow target to confirm the new tests fail if necessary, then
   implement only the minimal test/code adjustment required.
3. Use focused module coverage output to confirm the target module’s current
   missing opportunities are exercised by the new tests.
4. Add a second focused top-up only if needed based on the current artifact and
   targeted evidence.
5. Write `plans/PR614_CURRENT_FULL_COVERAGE_TOPUP_VALIDATION.md` with
   requirement status and focused command evidence.
6. Commit the scoped fix locally with a conventional CI-fix message.

## Assumptions/Changes

- The node Git manager top-up covers the original `node/git_manager.py` gaps
  from the current CI artifact, but that file represented roughly 30
  opportunities while the exact gate was 54 opportunities short. Add a second
  focused top-up in executor validation helper tests for the current artifact's
  command-record, metadata rehydration, artifact-read, and operator-facing
  failure-message gaps.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest <changed test target> -q`
- Optional focused coverage only for the changed module/test target:
  `uv run --python 3.12 --extra dev pytest <changed test target> --cov=<target-module> --cov-branch --cov-report=term-missing -q`

Pass criteria: focused tests pass, targeted coverage shows the intended
previously-missing behavior is now exercised, and broad full-coverage validation
is left to AWF/GitHub CI after this agent phase.
