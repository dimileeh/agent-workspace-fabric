# PR272 CI Full Coverage Gap Plan

## Problem Statement and Scope

PR #272's GitHub Actions `python-full-coverage` job passes all tests but fails
the required 99% coverage gate with 98.25% total coverage. The aggregate
`ci-required` job fails only because `python-full-coverage` failed.

Scope is limited to restoring real Python coverage for currently uncovered
production behavior. Do not disable, skip, weaken, or lower the coverage gate.

## Requirements Checklist

- Use the CI log/artifact evidence to target the true coverage shortfall.
- Add focused tests that execute uncovered behavior in touched production
  surfaces.
- Preserve existing protected quality-gate and worker behavior.
- Run focused repro/verification first, then broader validation only after the
  focused fix is in place.
- Keep AWF branch/push ownership intact; commit locally with a conventional
  `fix(ci): ...` message.

## Implementation Steps

1. Rank missing coverage from the downloaded CI `coverage.xml` artifact.
2. Add targeted tests for the highest-leverage uncovered branches, starting
   with `awf.control.quality_gates` and then small recently touched helper
   surfaces as needed.
3. Run focused tests with coverage for the touched modules and iterate until
   they close enough real misses to satisfy the global gate.
4. Run lint/type checks for touched Python files and, if practical, the same
   full coverage command used by CI.
5. Create `plans/PR272_CI_FULL_COVERAGE_GAP_VALIDATION.md` with requirement
   status and command evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py --cov=awf.control.quality_gates --cov-report=term-missing --cov-fail-under=99 -q`
  - Passes and shows the protected quality-gate module at or above 99%.
- Additional focused tests for any touched helper modules pass.
- `uv run --python 3.12 --extra dev ruff check <touched files>`
  - Passes with no lint regressions.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passes with no type regressions.
- `uv run --python 3.12 pytest -n 8 --dist=loadscope --timeout=300 --cov=awf --cov-report=term-missing --cov-report=xml --cov-fail-under=99`
  - Passes when local Docker/service prerequisites allow the CI-equivalent run.
