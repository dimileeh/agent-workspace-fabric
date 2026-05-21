# PR270 Full Coverage Fix Plan

## Problem Statement

PR #270's GitHub Actions `python-full-coverage` job passes all tests but fails
the required 99% coverage gate. The observed run completed 7,336 tests with
total coverage at 98.41%, so `ci-required` fails because it depends on that job.

## Scope

- Keep the CI coverage threshold and workflow behavior unchanged.
- Add focused test coverage for uncovered behavior introduced or touched by the
  capacity-scheduling work.
- Prefer tests that exercise real control-plane behavior rather than coverage-only
  assertions.
- Commit the fix locally on the existing AWF branch.

## Requirements Checklist

- [ ] Identify the uncovered changed-code paths contributing to the coverage drop.
- [ ] Add or update regression tests for the selected paths.
- [ ] Run focused tests for the changed test files and modules.
- [ ] Run enough coverage validation to show the 99% gate is restored, or document
      why a narrower validation was the strongest practical signal.
- [ ] Create `plans/PR270_FULL_COVERAGE_FIX_VALIDATION.md` with requirement status
      and command evidence.
- [ ] Commit the local fix with a conventional `fix(ci): ...` message.

## Implementation Steps

1. Inspect the CI coverage report and local source around missed changed lines.
2. Choose high-signal missing paths in the capacity queue, reservation, metrics, or
   config code where existing tests already provide fixtures.
3. Add tests that cover those paths through repository/service/worker APIs.
4. Run targeted pytest commands first, then broader coverage validation if runtime
   remains practical.
5. Update the validation document and commit the resulting files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest <focused tests> -q`
- `uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope --timeout=300 --cov=awf --cov-report=term-missing --cov-report=xml --cov-fail-under=99`

Pass criteria: focused tests pass, and the full coverage gate reaches at least 99%
without lowering or bypassing the check.
