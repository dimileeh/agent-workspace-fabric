# PR357 Coverage Gate Plan

## Problem Statement

PR #357 fails the GitHub Actions `python-full-coverage` job because the full
test suite passes but aggregate coverage is `98.99%`, just below the required
`99.00%` gate. The dependent `ci-required` job fails only because
`python-full-coverage` failed.

## Scope

- Do not edit workflow or coverage configuration.
- Do not weaken, skip, or disable the coverage gate.
- Keep the fix focused on real source coverage for an uncovered branch.
- Follow the AWF workspace contract: no branch switching, pushing, rebasing, or
  broad local validation.

## Requirements

- [ ] Identify a small uncovered branch from the CI coverage report.
- [ ] Add focused unit coverage for that branch without changing production
      behavior.
- [ ] Run targeted tests for the changed test file.
- [ ] Record validation evidence in `plans/PR357_COVERAGE_GATE_VALIDATION.md`.
- [ ] Commit the fix locally with a conventional CI-fix message.

## Implementation Steps

1. Use the GitHub Actions log to confirm the failure is coverage threshold only.
2. Add a focused unit test for an uncovered helper branch in
   `awf.service.locks`.
3. Run the narrowest relevant pytest command for the touched test file.
4. Save validation evidence and commit the scoped changes.

## Verification

Targeted command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_locks.py -q
```

Pass criteria: the targeted lock-service unit tests pass. Full AWF/GitHub
coverage validation remains managed by AWF after agent completion.
